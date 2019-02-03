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


#sox -t wav -r 44100 ./DTMF.wav -esigned-integer -b16 -r 22050 -t raw - | tee ./recode.raw | sox -esigned-integer -b16 -r 22050 -t raw - -t wav - | tee ./recode.wav | play -v 0 -

#dump the incoming call over STD out
def callHandler(dummyFile="", sound=False):
  #Play dummy wav file
  #dummyWav = mainDir + "/samples/JustOne.wav"
  if dummyFile == "":
    dummyWav = mainDir + "/samples/DTMF.wav"
  else:
    dummyWav = dummyFile
  rawFile = mainDir + "/calls/encoded.raw"
  callSave = mainDir + "/calls/call.wav"
  #dump dummyWav over STD out as raw and signed-integer
  bashCMD = f"sox -t wav -r 44100 {dummyWav} -esigned-integer -b16 -r 22050 -t raw - "
  #dump STD out to file
  bashCMD = bashCMD + f"| tee {rawFile} "
  #convert back to wav file
  bashCMD = bashCMD + f"| sox -esigned-integer -b16 -r 22050 -t raw - -t wav - 2>/dev/null"
  #dump wav file to saved calls
  bashCMD = bashCMD + f"| tee {callSave} "
  #control playback speed with play
  if sound:
    bashCMD = bashCMD + f"| play - &>/dev/null"
  else:
    bashCMD = bashCMD + f"| play -v 0 - &>/dev/null"
  #reset call when done
  bashCMD = bashCMD + f";sleep .2;echo '' > {rawFile}"
  
  #pipe raw data to multimon-ng
  tailCMD = f"tail -f {rawFile}"
  tailCMD = tailCMD + "| multimon-ng -t raw -a DTMF -"
  #print (bashCMD)
  runningHandle = subprocess.Popen(bashCMD,
                                   preexec_fn=os.setsid,
                                   universal_newlines=True, shell=True)
  tailHandle = subprocess.Popen(tailCMD,
                                preexec_fn=os.setsid,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True, shell=True)
  for line in tailHandle.stdout:
    if "file truncated" in line:
      return
    #print("INFO:")
    if "DTMF:" in line:
      yield line


#dummy = mainDir + "/samples/DTMF.wav"
dummy = mainDir + "/samples/test2.wav"
for line in callHandler(dummyFile=dummy, sound=False):
  #pass
  print(line)
