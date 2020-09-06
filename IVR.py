#!/usr/bin/python3

#WIP, this is not done yet!

#This file is run by callen.py, This is were you define how the IVR works. 
#record_call(), ring_phone() and hangup() all exit the script
while True:
    say("Welcome to my phone, press 1 if you're human, press 2 to leave a message")
    #read one DTMF tone key
    reply = DTMF(1)[0]
    if reply == '1':
        say("Good, humans are my favorite", repeat=False)
        ring_phone()
    elif reply == '2':
        say("Leave you message now.")
        record_call()
    else:
        say("Invalid reply.", repeat=False)
