#!/usr/bin/python3

#WIP, this is not done yet!
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


#This file is run by callen.py, This is were you define how the IVR works. 
#record_call(), ring_phone() and hangup() all exit the script
def IVR():
    say("Welcome to my phone, press 1 if you're human, press 2 to leave a message")
    while True:
        #read one DTMF tone key
        reply = DTMF(1)[0]
        if reply == '1':
            say("Good, humans are my favorite", repeat=False)
            ring_phone()
        elif reply == '2':
            record_call()
        else:
            say("Invalid reply.", repeat=False)
            say("Welcome to my phone, press 1 if you're human, press 2 to leave a message")

