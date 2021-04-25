#!/usr/bin/python3
#Callen GPL3
#Copyright (C) 2020 David Hamner

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

#sudo apt install espeak multimon-ng sox pulseaudio-utils

import os
import subprocess
import threading
import time
import datetime

mainDir = os.path.dirname(os.path.realpath(__file__))
callDir = mainDir + "/calls"

#pacat --format=s16ne --channels=1 --rate=48000 --record -d alsa_output.platform-sound.Audio__hw_CARD_wm8962__sink.monitor | sox -t raw -r 48000 -e signed-integer -b 16 -c 1 - -t raw -r 22050 - | multimon-ng -t raw -a DTMF -
#tail -f -n +1 ./recode.wav | play -

#audio_in = "alsa_output.platform-sound.Audio__hw_CARD_wm8962__sink.monitor"
audio_in = "alsa_output.platform-sound.HiFi__hw_CARD_wm8962__sink.monitor"
audio_out = "alsa_output.platform-sound-wwan.stereo-fallback"
script_path = os.path.dirname(os.path.realpath(__file__))

#reinit module-role-cork
def setup_audio():
    bashCMD = f"pactl unload-module module-role-cork"
    os.system(bashCMD)
    bashCMD = f"pactl load-module module-role-cork"
    os.system(bashCMD)

#dump the incoming call over STD out
def callHandler():
  callSave = mainDir + "/calls/call.raw"
  
  #Cat audio_in over STD out, tee to file, conver to something multimon-ng can read... and read it.
  bashCMD = f"pacat --format=s16ne --channels=1 --rate=48000 --record -d  {audio_in} "
  bashCMD = bashCMD + f"| tee {callSave} "
  bashCMD = bashCMD + f"| sox -t raw -r 48000 -e signed-integer -b 16 -c 1 - -t raw -r 22050 -  "

  #Read DTMF
  bashCMD = bashCMD + f"| multimon-ng -t raw -a DTMF -"
  
  print(f"Running: {bashCMD}")
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
    bashCMD = f"kill -9 $(pgrep -f {audio_out})"
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
            print("Force stop say thread")
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
        print(f"bash_say: {what_to_say}")
        if getattr(t, "terminate", False):
            print("Terminate bash_say")
            break
        bashCMD = f'espeak --stdout "{what_to_say}" - '
        bashCMD = bashCMD + f"| sox -t wav -r 22050 - -esigned-integer -b16 -c 1 -r 96000 -t raw - "
        bashCMD = bashCMD + f"| pacat -d {audio_out} -p "
        print(bashCMD)
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
    callSave = mainDir + f"/calls/{str(datetime.datetime.now())}_call.wav"
    
    say("Press # when you'er done. Beep!", repeat=False)
    #Cat audio_in over STD out, tee to file, conver to something multimon-ng can read... and read it.
    bashCMD = f"pacat --format=s16ne --channels=1 --rate=48000 --record -d  {audio_in} "
    bashCMD = bashCMD + f"| sox -t raw -r 48000 -L -e signed-integer -S -b 16 -c 1 - '{callSave}' rate 48000"
    #run command in backround
    runningHandle = subprocess.Popen(bashCMD,
                                preexec_fn=os.setsid,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True, shell=True)
    #wait for #key
    while DTMF(1)[0] != '#':
        pass
    #end command
    runningHandle.terminate()
    say("Thank you, good bye", repeat=False)
    hangup()

def hangup():
    #echo -ne "AT+CHUP\r\n" | tee /dev/ttyUSB3
    bashCMD = f'echo -ne "AT+CHUP\r\n" | sudo tee /dev/ttyUSB3'
    #run command in backround
    runningHandle = subprocess.Popen(bashCMD,
                                preexec_fn=os.setsid,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True, shell=True)

#Setup IVR script:
script = ""
ivr_script = script_path + "/IVR.py"
for python_line in open(ivr_script).readlines():
    script = script + python_line


#Pickup calls and run IVR.py
#sudo cat /dev/ttyUSB3
#Look for:
#+CLIP: "+15030000000",145,,,,0
#RUN:
#echo -ne "ATA\r\n" | sudo tee /dev/ttyUSB3

#clean up old processes if needed:
bashCMD2 = f"for i in $(pgrep -f /dev/ttyUSB3); do sudo kill $i; done"
os.system(bashCMD2)

bashCMD = f'sudo cat /dev/ttyUSB3'
#bashCMD = bashCMD + f"| grep CLIP"
    

runningHandle = subprocess.Popen(bashCMD,
                            preexec_fn=os.setsid,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            universal_newlines=True, shell=True)

#Fix for https://source.puri.sm/Librem5/librem5-base/-/blob/pureos/byzantium/default/audio/pulse/librem5.pa#L22
setup_audio()

while True:
        output = runningHandle.stdout.readline()
        if output:
            if "DISC" in output:
                print("Call ended! Stoping IVR")
                IVR_thread.terminate = True
            elif "CLIP" in output:
                phone_number = output.split('"')[1]
                print("Call from: " + phone_number)
                
                #answer call
                os.system('echo -ne "ATA\r\n" | sudo tee /dev/ttyUSB3')
                
                #Run ivr_script
                try:
                    exec(script)
                except Exception as e:
                    print('Error Loading IVR' + ivr_script)
                    #Error loading, Exit with error
                    raise e
                    #exit()
                #Run IVR() from ivr_script
                IVR_thread = threading.Thread(target=IVR, args=())
                IVR_thread.daemon = True
                IVR_thread.start()
            #else:
                #print("Debug: " + output)


