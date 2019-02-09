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

import os
import subprocess

mainDir = os.path.dirname(os.path.realpath(__file__))
callDir = mainDir + "/calls"


#cat ./DTMF.wav | tee ./recode.wav | sox -t wav -r 44100 - -esigned-integer -b16 -r 22050 -t raw - | pv -qL 44100 | multimon-ng -t raw -a DTMF -

#tail -f -n +1 ./recode.wav | play -


#dump the incoming call over STD out
def callHandler(wavFile="", sound=False):
  #Play dummy wav file
  #wavFile = mainDir + "/samples/JustOne.wav"
  if wavFile == "":
    wavFile = mainDir + "/samples/DTMF.wav"
  
  #TODO remove rawFile
  rawFile = mainDir + "/calls/encoded.raw"
  callSave = mainDir + "/calls/call.wav"
  #touch rawFile
  #callSave, also
  
  #dump wavFile over STD out as wav, tee to file, conver to something multimon-ng can read... and read it.
  bashCMD = f"cat {wavFile} "
  bashCMD = bashCMD + f"| tee {callSave} "
  bashCMD = bashCMD + f"| sox -t wav -r 44100 - -esigned-integer -b16 -r 22050 -t raw - "

  #control speed of STD out
  bashCMD = bashCMD + f"| pv -qL 44100 "
  #Read DTMF
  bashCMD = bashCMD + f"| multimon-ng -t raw -a DTMF -"
  
  #print(bashCMD)
  if sound:
    #pipe wav data to play cmd (if needed) 
    soundCMD = f"tail -f -n +1 {callSave}"
    soundCMD = soundCMD + "| play -"
  
  #run multimon-ng
  runningHandle = subprocess.Popen(bashCMD,
                                   preexec_fn=os.setsid,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   universal_newlines=True, shell=True)
  if sound:
    tailHandle = subprocess.Popen(soundCMD,
                                  preexec_fn=os.setsid,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  universal_newlines=True, shell=True)
  for line in runningHandle.stdout:
    if "file truncated" in line:
      return
    #print("INFO:")
    if "DTMF:" in line:
      yield line


dummy = mainDir + "/samples/DTMF.wav"
#dummy = mainDir + "/samples/test2.wav"
for line in callHandler(wavFile=dummy, sound=True):
  #pass
  print(line)
