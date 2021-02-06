# MIT License
#
# Copyright (c) 2020 Oli Wright <oli.wright.github@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# mqtt_devices.py
#
# Wrapper for a set of MQTT virtual devices.
# This can be used to MQTT-enable legacy devices that can only be operated by
# IR, or older less well-supported protocols.

import paho.mqtt.client as mqtt
import signal
import time
import threading

devices = None

# Condition variable that is used to wake up the main loop
# when there might be things for it to do.
main_thread_cv = threading.Condition()
alarm_clock_cv = threading.Condition()

def wake_up_cv(cv):
	cv.acquire()
	cv.notify()
	cv.release()

def wait_for_cv(cv, timeout = None):
	cv.acquire()
	cv.wait(timeout)
	cv.release()

# Kill signal handling
quit = False
def receiveKillSignal(signal_number, frame):
	print("Received kill signal")
	# Set our global 'quit' flag to True
	global quit
	quit = True
	# Wake up the main loop
	global main_thread_cv
	wake_up_cv(main_thread_cv)

signal.signal(signal.SIGINT,  receiveKillSignal)
signal.signal(signal.SIGTERM, receiveKillSignal)

alarm_time = None
alarm_triggered = False
def alarm_clock_thread_func():
	global alarm_time
	global alarm_triggered
	global quit
	while not quit:
		# Should we sleep?
		sleep_time = None
		if alarm_time is not None:
			now = time.monotonic()
			time_until_alarm = alarm_time - now
			if time_until_alarm < 0.1:
				# Within 1/10s
				alarm_triggered = True
				alarm_time = None
				# Wake up the main thread
				wake_up_cv(main_thread_cv)
				#print("DingDong")
			else:
				sleep_time = time_until_alarm
		#if sleep_time is not None:
		#	print("Alarm sleeping for " + str(sleep_time))
		#else:
		#	print("Alarm sleeping indefinitely")
		wait_for_cv(alarm_clock_cv, sleep_time)

def set_alarm_time(time_for_alarm):
	global alarm_time
	if (alarm_time is None) or (time_for_alarm < alarm_time):
		#print("Alarm set in " + str((time_for_alarm - time.monotonic())))
		alarm_time = time_for_alarm
		# Wake the alarm clock, so it can reset itself
		wake_up_cv(alarm_clock_cv)

# The callback for when the client receives a CONNACK response from the server.
def on_connect(client, userdata, flags, rc):
	print("Connected to MQTT broker with result code {result}".format(result = str(rc)))
	client.publish("home/mqtt-devices/status", payload = "online", retain = True)

	# Subscribing in on_connect() means that if we lose the connection and
	# reconnect then subscriptions will be renewed.
	global devices
	for device in devices:
		if hasattr(device, 'subscribe_topic'):
			print("Subscribing to " + device.subscribe_topic)
			client.subscribe(device.subscribe_topic)
		if hasattr(device, 'on_connect'):
			device.on_connect(client, userdata, flags, rc)

# The callback for when a PUBLISH message is received from the server.
def on_message(client, userdata, msg):
	payload = msg.payload.decode("utf-8")
	#print("Received {topic} {payload}".format(topic = msg.topic, payload = payload))
	global devices
	for device in devices:
		if hasattr(device, 'base_topic') and msg.topic.startswith(device.base_topic):
			device.on_message(client, userdata, msg, payload)
	# Wake up the main thread in case this message will result
	# in any work needing to be done.
	global main_thread_cv
	wake_up_cv(main_thread_cv)

def on_log(mqttc, obj, level, string):
	print(string)

def Run(local_devices, mqtt_credentials):
	global devices
	global alarm_clock_cv
	global main_thread_cv
	devices = local_devices

	# Establish a connection to the broker
	client = mqtt.Client()
	client.on_connect = on_connect
	client.on_message = on_message
	#client.on_log = on_log
	client.username_pw_set(mqtt_credentials["User"], password = mqtt_credentials["Password"])
	client.will_set("home/mqtt-devices/status", payload = "offline", retain=True)
	client.connect_async(mqtt_credentials["IP"], mqtt_credentials["Port"], keepalive=60)

	# Start the mqtt client thread
	client.loop_start()

	# Start the alarm clock thread
	alarm_clock_thread = threading.Thread(target = alarm_clock_thread_func)
	alarm_clock_thread.start()

	# Main loop
	# Update all devices that need updating, when they want updating
	global quit
	while not quit:
		# Update all the devices, and establish the next update time
		#print("Main thread updating")

		now = time.monotonic()
		next_update_time = None
		for device in devices:
			if hasattr(device, 'on_update'):
				# This device has an update method. Does it need updating now?
				do_update = False
				has_next_update_time = hasattr(device, 'next_update_time') and (device.next_update_time is not None)
				if has_next_update_time:
					do_update = device.next_update_time < (now + 0.1)
				else:
					# Maybe this device wants us to do updates whenever the main loop ticks around
					do_update = hasattr(device, 'do_adhoc_updates') and (device.do_adhoc_updates == True)

				if do_update:
					device.on_update(client)

				if has_next_update_time and ((next_update_time is None) or (device.next_update_time < next_update_time)):
					# Set the next update time of the main loop to this device's
					# next update time.
					next_update_time = device.next_update_time

		if next_update_time is not None:
			# Set an alarm to wake up the main loop for the next update.
			# Messages can still force an earlier update if required.
			set_alarm_time(next_update_time)

		# Go to sleep until something wakes up the main thread condition variable
		wait_for_cv(main_thread_cv)

	# If we get here, then it's because we've been asked to quit.
	# Stop the mqtt client thread and exit
	print("Waking up the alarm clock to kill it")
	wake_up_cv(alarm_clock_cv)
	alarm_clock_thread.join()
	print("Stopping the client loop")
	client.loop_stop()
