#!/usr/bin/env python

import paho.mqtt.client as paho  # pip install paho-mqtt
import broadlink  # pip install broadlink
import os
import sys
import time
import logging
import logging.config
import socket
import sched
from threading import Thread

# read initial config files
dirname = os.path.dirname(os.path.abspath(__file__)) + '/'
logging.config.fileConfig(dirname + 'logging.conf')
CONFIG = os.getenv('BROADLINKMQTTCONFIG', dirname + 'mqtt.conf')


class Config(object):
    def __init__(self, filename=CONFIG):
        self.config = {}
        execfile(filename, self.config)

    def get(self, key, default='special empty value'):
        v = self.config.get(key, default)
        if v == 'special empty value':
            logging.error("Configuration parameter '%s' should be specified" % key)
            sys.exit(2)
        return v


try:
    cf = Config()
except Exception, e:
    print "Cannot load configuration from file %s: %s" % (CONFIG, str(e))
    sys.exit(2)

qos = cf.get('mqtt_qos', 0)
retain = cf.get('mqtt_retain', False)

topic_prefix = cf.get('mqtt_topic_prefix', 'broadlink/')


# noinspection PyUnusedLocal
def on_message(mosq, device, msg):
    command = msg.topic[len(topic_prefix):]
    if command == 'temperature':  # internal notification
        return

    logging.debug("Received MQTT message " + msg.topic + " " + str(msg.payload))
    file = dirname + "commands/" + command
    action = str(msg.payload)

    try:
        if action == '' or action == 'auto':
            record_or_replay(device, file)
        elif action == 'record':
            record(device, file)
        elif action == 'replay':
            replay(device, file)
        else:
            logging.debug("Unrecognized MQTT message " + action)
    except Exception:
        logging.exception("I/O error")


# noinspection PyUnusedLocal
def on_connect(mosq, device, result_code):
    topic = topic_prefix + '#'
    logging.debug("Connected to MQTT broker, subscribing to topic " + topic)
    mqttc.subscribe(topic, qos)


# noinspection PyUnusedLocal
def on_disconnect(mosq, device, rc):
    logging.debug("OOOOPS! Broadlink disconnects")
    time.sleep(10)


def record_or_replay(device, file):
    if os.path.isfile(file):
        replay(device, file)
    else:
        record(device, file)


def record(device, file):
    logging.debug("Recording command to file " + file)
    # receive packet
    device.enter_learning()
    ir_packet = None
    attempt = 0
    while ir_packet is None and attempt < 6:
        time.sleep(5)
        ir_packet = device.check_data()
        attempt = attempt + 1
    if ir_packet is not None:
        # write to file
        directory = os.path.dirname(file)
        if not os.path.exists(directory):
            os.makedirs(directory)
        with open(file, 'wb') as f:
            f.write(str(ir_packet).encode('hex'))
        logging.debug("Done")
    else:
        logging.warn("No command received")


def replay(device, file):
    logging.debug("Replaying command from file " + file)
    with open(file, 'rb') as f:
        ir_packet = f.read()
    device.send_data(ir_packet.decode('hex'))


def get_device(cf):
    device_type = cf.get('device_type', 'lookup')
    if device_type == 'lookup':
        local_address = cf.get('local_address', None)
        lookup_timeout = cf.get('lookup_timeout', 20)
        devices = broadlink.discover(timeout=lookup_timeout) if local_address is None else \
            broadlink.discover(timeout=lookup_timeout, local_ip_address=local_address)
        if len(devices) == 0:
            logging.error('No Broadlink device found')
            sys.exit(2)
        if len(devices) > 1:
            logging.error('More than one Broadlink device found (' + ', '.join([d.host for d in devices]) + ')')
            sys.exit(2)
        return devices[0]
    elif device_type == 'test':
        class TestDevice:
            type = 'test'
            host = 'test'
            def auth(self):
                pass
            def check_temperature(self):
                return 23.5
        return TestDevice()
    else:
        host = (cf.get('device_host'), 80)
        mac = bytearray.fromhex(cf.get('device_mac').replace(':', ' '))
        if device_type == 'rm':
            return broadlink.rm(host=host, mac=mac)
        elif device_type == 'sp1':
            return broadlink.sp1(host=host, mac=mac)
        elif device_type == 'sp2':
            return broadlink.sp2(host=host, mac=mac)
        elif device_type == 'a1':
            return broadlink.a1(host=host, mac=mac)
        elif device_type == 'mp1':
            return broadlink.mp1(host=host, mac=mac)
        else:
            logging.error('Incorrect device configured: ' + device_type)
            sys.exit(2)


def broadlink_rm_temperature_timer(scheduler, delay, device):
    scheduler.enter(delay, 1, broadlink_rm_temperature_timer, [scheduler, delay, device])

    temperature = str(device.check_temperature())
    topic = topic_prefix + "temperature"
    logging.debug("Sending RM temperature " + temperature + " to topic " + topic)
    mqttc.publish(topic, temperature, qos=qos, retain=retain)


class TimerThread(Thread):
    def __init__(self, s):
        Thread.__init__(self)
        self.s = s

    def run(self):
        self.s.run()


if __name__ == '__main__':
    device = get_device(cf)
    device.auth()
    logging.debug('Connected to %s Broadlink device at %s' % (device.type, device.host))

    clientid = cf.get('mqtt_clientid', 'broadlink-%s' % os.getpid())
    # initialise MQTT broker connection
    mqttc = paho.Client(clientid, clean_session=cf.get('mqtt_clean_session', False), userdata=device)

    mqttc.on_message = on_message
    mqttc.on_connect = on_connect
    mqttc.on_disconnect = on_disconnect

    mqttc.will_set('clients/broadlink', payload="Adios!", qos=0, retain=False)

    # Delays will be: 3, 6, 12, 24, 30, 30, ...
    # mqttc.reconnect_delay_set(delay=3, delay_max=30, exponential_backoff=True)

    mqttc.username_pw_set(cf.get('mqtt_username'), cf.get('mqtt_password'))
    mqttc.connect(cf.get('mqtt_broker', 'localhost'), int(cf.get('mqtt_port', '1883')), 60)

    broadlink_rm_temperature_interval = cf.get('broadlink_rm_temperature_interval', 0)
    if broadlink_rm_temperature_interval > 0:
        scheduler = sched.scheduler(time.time, time.sleep)
        scheduler.enter(broadlink_rm_temperature_interval, 1, broadlink_rm_temperature_timer, [scheduler, broadlink_rm_temperature_interval, device])
        # scheduler.run()
        tt = TimerThread(scheduler)
        tt.daemon = True
        tt.start()

    while True:
        try:
            mqttc.loop_forever()
        except socket.error:
            time.sleep(5)
        except KeyboardInterrupt:
            sys.exit(0)
