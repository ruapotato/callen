#!/usr/bin/python3
#Callen GPL3
#Copyright (C) 2019 David Hamner

#This program is free software: you can redistribute it and/or modify
#it under the terms of the GNU General Public License as published by
#the Free Software Foundation, either version 3 of the License, or
#(at your option) any later version.

#This program is distributed in the hope that it will be useful,
#but WITHOUT ANY WARRANTY; without even the implied warranty of
#MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#GNU General Public License for more details.

#You should have received a copy of the GNU General Public License
#along with this program. If not, see <http://www.gnu.org/licenses/>.

#sudo apt install espeak multimon-ng pacat sox

import os
import subprocess
import threading
import time

mainDir = os.path.dirname(os.path.realpath(__file__))
callDir = mainDir + "/calls"

#pacat --format=s16ne --channels=1 --rate=48000 --record -d alsa_output.platform-sound.Audio__hw_CARD_wm8962__sink.monitor | sox -t raw -r 48000 -e signed-integer -b 16 -c 1 - -t raw -r 22050 - | multimon-ng -t raw -a DTMF -
#tail -f -n +1 ./recode.wav | play -

audio_in = "alsa_output.platform-sound.Audio__hw_CARD_wm8962__sink.monitor"
audio_out = "alsa_output.platform-sound-wwan.stereo-fallback"
script_path = os.path.dirname(os.path.realpath(__file__))

#dump the incoming call over STD out
def callHandler():
  callSave = mainDir + "/calls/call.raw"
  
  #Cat audio_in over STD out, tee to file, conver to something multimon-ng can read... and read it.
  bashCMD = f"pacat --format=s16ne --channels=1 --rate=48000 --record -d  {audio_in} "
  bashCMD = bashCMD + f"| tee {callSave} "
  bashCMD = bashCMD + f"| sox -t raw -r 48000 -e signed-integer -b 16 -c 1 - -t raw -r 22050 -  "

  #Read DTMF
  bashCMD = bashCMD + f"| multimon-ng -t raw -a DTMF -"
  
  #run multimon-ng
  runningHandle = subprocess.Popen(bashCMD,
                                   preexec_fn=os.setsid,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   universal_newlines=True, shell=True)

  for line in runningHandle.stdout:
    if "file truncated" in line:
      return
    #print("INFO:")
    if "DTMF:" in line:
      yield line.split(":")[-1].strip()

#stop all ative say() commands
def force_stop():
    global thread
    thread.terminate = True
<<<<<<< HEAD
    bashCMD = f"kill -9 $(pgrep -f {audio_out})"
=======
<<<<<<< HEAD
    bashCMD = f"kill -9 $(pgrep -f {audio_out})"
=======
    bashCMD = "kill -9 $(pgrep -f espeak) 2> /dev/null"
>>>>>>> 9b7ba4760396c1232c9132abb7862b609ed992ea
>>>>>>> 28384752c708fa6d462aca0d4b2293699ca12061
    os.system(bashCMD)
    
#Read x number of DTMF keys
def DTMF(number_of_keys):  
    return_data = []
    for DTMF_code in callHandler():
        return_data.append(DTMF_code)
        if len(return_data) >= number_of_keys:
            #Stop any active say commands
            force_stop()
            
            return(return_data)

def say(what_to_say, repeat=True):
    global thread
    try:
        if thread.isAlive():
            force_stop()
    except NameError:
        pass
    if repeat:
        thread = threading.Thread(target=bash_say, args=(what_to_say, repeat))
        thread.daemon = True
        thread.start()
    else:
        bash_say(what_to_say, repeat)
#used by say()
def bash_say(what_to_say, repeat):
    t = threading.currentThread()
    while True:
        if getattr(t, "terminate", False):
            break
        bashCMD = f'espeak --stdout "{what_to_say}" - '
        bashCMD = bashCMD + f"| sox -t wav -r 22050 - -esigned-integer -b16 -c 1 -r 96000 -t raw - "
        bashCMD = bashCMD + f"| pacat -d {audio_out} -p "
        runningHandle = subprocess.Popen(bashCMD,
                                        preexec_fn=os.setsid,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT,
                                        universal_newlines=True, shell=True)
        if not repeat:
            runningHandle.communicate()
            return()
        else:
            runningHandle.communicate()
            time.sleep(4)

    #espeak --stdout "Hello world" - | sox -t wav -r 22050 - -esigned-integer -b16 -c 1 -r 96000 -t raw - | pacat -d alsa_output.platform-sound-wwan.stereo-fallback -p

def ring_phone():
    pass #TODO
def record_call():
    pass #TODO
def hangup():
    pass #TODO


#RUN IVR.py
script = ""
ivr_script = script_path + "/IVR.py"
for python_line in open(ivr_script).readlines():
    script = script + python_line

try:
    exec(script)
except Exception as e:
    print('Error Loading IVR' + ivr_script)
    #Error loading, Exit with error
    raise e
    #exit()
        
        


#say("Enter the pin")
#while True:
#    key = DTMF(1)
#    print(key)
#    say("Hello " + key[0], repeat=False)
#    say(f"{key}, Enter the pin again")

#for line in callHandler():
  #pass
#  print(line)

