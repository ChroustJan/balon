#!/usr/bin/python

# Monitor script

# GPS part from code by Dan Mandle http://dan.mandle.me September 2012, License: GPL 2.0 

import time
import datetime
import sys
import logging 
import re

import os
from gps import *
from time import *
import time
import threading

import pygame
import pygame.camera
#from pygame.locals import *

## Modem
from gsmmodem.modem import GsmModem, SentSms
from gsmmodem.exceptions import InterruptedException, PinRequiredError, IncorrectPinError, TimeoutException
###

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] (%(threadName)-10s) %(message)s',
                    )
gpsd = None #seting the global variable

from pymlab import config

#### Settings #####
data_dir="/data/balon/"
default_destination = "+420777642401"

round_beat = 10 # Seconds for one round of sensors capture

# Webcam #
#webcam_enabled=True
webcam_enabled=False
imagedir=data_dir+"img/"
video_device="/dev/video0"
resolutionx=640 # Max 1600
resolutiony=480 # Max 480
skipframes=5
beattime=60

# GSM module #
PORT = '/dev/ttyACM99'
BAUDRATE = 9600 # Higher baud rates than 9600 lead to errors
PIN = None # SIM card PIN (if any)
####################################################################

def make_selfie():
  logging.debug("Initializing camera at {0} for {1}x{2} px JPEG every {3} s.".format(video_device,resolutionx,resolutiony,beattime))
  try:
    cam = pygame.camera.Camera(video_device,(resolutionx,resolutiony))
    cam.start()

  except SystemError as e:
    logging.error(e)
    return(1)

  # Wait for initialization
  while (not cam.query_image()):
    time.sleep(0.1)

  # Skip required number of frames
  if (skipframes > 0):
    logging.debug("Waiting for image stabilization - skipping {0} frames.".format(skipframes))
    for i in range(skipframes):
      try:
        img = cam.get_image()
      except pygame.error:
        logging.error("Error during frame capture: {0}".format(sys.exc_info()[0]))
        return(2)

  # Construct file name
  savefname=time.strftime(imagedir+'%F_%T.jpg', time.gmtime())
  logging.info("Capturing {0}x{1} frame to {2}.".format(resolutionx,resolutiony,savefname))
  try:
    ensure_dir(savefname)
    img = cam.get_image()
    pygame.image.save(img, savefname)
    cam.stop()
  except pygame.error as e:
    logging.error("Capture failed: {0}".format(e))

####################################################################
class WebCamCapture(threading.Thread):
  def __init__(self):
    logging.debug("WebCam thread initialization")
    threading.Thread.__init__(self)
    self.running = True
    self.name = 'WebCam'

  def run(self):
    logging.debug("Thread starting")
    while webcam.running:
      zerotime=time.time()
      make_selfie()
      selfie_time=time.time()-zerotime
      if (selfie_time > beattime):
        logging.warn("Webcam image capture takes too long: {0} s, can not pad the beats to {1}.".format(selfie_time, beattime))
      else:
        logging.debug("Capture took {0} s. Next beat in {1} s to pad to {2} s.".format(selfie_time, beattime-selfie_time, beattime))
        time.sleep(beattime-selfie_time)

    logging.debug("Thread exiting")

#### ensure_dir ####
def ensure_dir(f):
    d = os.path.dirname(f)
    if not os.path.exists(d):
          logging.info("Creating directory: {0}".format(d))
          os.makedirs(d)

#### GPS Poller #####################################################
class GpsPoller(threading.Thread):
  def __init__(self):
    logging.info("Starting GPS poller thread")
    threading.Thread.__init__(self)
    global gpsd #bring it in scope
    gpsd = gps(mode=WATCH_ENABLE) #starting the stream of info
    self.current_value = None
    self.running = True #setting the thread running to true

  def run(self):
    logging.info("GPS poller thread running")
    global gpsd
    while gpsp.running:
      gpsd.next() #this will continue to loop and grab EACH set of gpsd info to clear the buffer

#### Send position in a SMS ########################################
def send_position_via_sms(destination):
    logging.info("Will send position via SMS to: {0}.".format(destination))
    try:
       # Get a GPS fix, prepare a string with it
       if (sensors['GPS_Fix'] < 3):
         smstext = "{0} GSM: {1}".format(gpsd.utc, sensors['GSM_CellInfo']) 
       else:
         smstext = "{0} alt{1} http://www.google.com/maps/place/{2},{3}".format(gpsd.utc, gpsd.fix.altitude, gpsd.fix.latitude,gpsd.fix.longitude)

       smstext = smstext + (" BT{0} BCH{1} ".format(sensors['Bat_Temp'], sensors['Bat_Charge']))

       sms = modem.sendSms(destination, smstext, waitForDeliveryReport=True)
    except TimeoutException:
       logging.warn('Failed to send message to {0}: the send operation timed out'.format(call.number))
    else:
       if sms.report:
            logging.info('Message sent{0}'.format(' and delivered OK.' if sms.status == SentSms.DELIVERED else ', but delivery failed.'))
       else:
            logging.info('Message sent.')


#### Incoming call handler #########################################
def handleIncomingCall(call):
    if call.ringCount == 1:
        logging.info('New incoming call, waiting for CLIP.'.format(call.number))
    elif call.ringCount <= 3 and not (call.number is None):
        logging.info('Got CLIP, Will send position to {0}.'.format(call.number))
        destination=call.number
        call.hangup()
        send_position_via_sms(destination)
    elif call.ringCount > 3:
        call.hangup()
        send_position_via_sms(default_destination)
    else:
        logging.info('Call is ringing and we still have no CLIP.')
         

class ModemHandler(threading.Thread):
  # +CGED: MCC:230, MNC:  3, LAC:878c, CI:2a95,
  CGED_REGEX = re.compile(r'^\+CGED:\s*MCC:([^,]+),\s*MNC:\s*([^,]+),\s*LAC:\s*([^,]+),\s*CI:\s*([^,]+),.*')
  def __init__(self):
    logging.info("Starting Modem handler thread")
    threading.Thread.__init__(self)
    global modem
    self.running = True #setting the thread running to true
    self.name = "Modem"
    self.networkName = "none"
    self.signalStrength = -1
    self.cellInfo = "none"

  def run(self):
    logging.info("Modem thread running")
    global modem
    rxListenLength = 10
    init_count = 0
    
    while self.running and init_count > -1:
      try:
        init_count = init_count + 1
        modem = GsmModem(PORT, BAUDRATE, incomingCallCallbackFunc=handleIncomingCall)
        logging.info("Initializing modem, try {0}.".format(init_count))
        modem.connect(PIN)
        init_count = -1
      except TimeoutException as e:
        logging.critical("Failed to initialize the GSM module: {0}", e)
        modem.close()

    logging.info('Waiting for incoming calls...')
    while self.running:
      try:
        #waitForNetworkCoverage()
        self.signalStrength = modem.signalStrength
        self.networkName = modem.networkName
        serving_cell=self.CGED_REGEX.match(modem.write("AT+CGED=3")[0])
        if serving_cell:
          mcc=serving_cell.group(1)
          mnc=serving_cell.group(2)
          lac=serving_cell.group(3)
          ci=serving_cell.group(4)
          self.cellInfo="{0}/{1}/{2}/{3}".format(mcc, mnc, lac, ci)

        # Comms are handled elsewhere so we could eventually just sleep, waiting
        #time.sleep(rxListenLength)
        modem.rxThread.join(rxListenLength) 
      except (InterruptedException, PinRequiredError, IncorrectPinError, TimeoutException):
        logging.error("rxThread died: {0}".format(sys.exc_info()[0]))

    modem.close()
    logging.info("Modem closed.")

#### Script Arguments ###############################################

cfg_number = 0
port = 4
sensors = {}

#### Sensor Configuration ###########################################

cfglist=[
    config.Config(
        i2c = {
            "port": port,
        },

        bus = [
            {
                "type": "i2chub",
                "address": 0x73,
                
                "children": [
                    {"name": "altimet", "type": "altimet01" , "channel": 0, },   
        	    	{"name": "sht25", "type": "sht25", "channel": 5, },
                    {"name": "guage", "type": "lioncell", "channel": 3, },
                ],
            },
        ],
    ),
]

try:
    cfg = cfglist[cfg_number]
except IndexError:
    sys.stdout.write("Invalid configuration number.")
    sys.exit(1)

cfg.initialize()
altimet = cfg.get_device("altimet")
sht_sensor = cfg.get_device("sht25")
guage = cfg.get_device("guage")
time.sleep(0.5)

# GPS thread initialization and startup
gpsp = GpsPoller() # create the thread
gpsp.start() # start it up

# Webcam thread initialization and startup
if webcam_enabled:
  logging.info("Initializing image capture.")
  pygame.init()
  pygame.camera.init()
  webcam = WebCamCapture()
  webcam.looptime = beattime
  webcam.start()
else:
  logging.info("Webcam image capture disabled.")

# GSM call/sms handler
logging.info("Initializing GSM support.")
gsmpart = ModemHandler()
gsmpart.start()

#### 

#### Data Logging ###################################################

sys.stdout.write("# Data acquisition system started \n")

#gpsp.join()


try:
    with open(data_dir+"data_log.csv", "a") as f:
	f.write("\nEpoch\tGPS_date_UTC\tGPS_fix\tGPS_alt\tLatitude\tLongitude\tGSM_signal\tGSM_CellInfo\tT_CPU\tT_Altimet\tPressure\tT_SHT\tHumidity\tT_Bat\tRemCap_mAh\tCap_mAh\tU_mV\tI_mA\tCharge_pct\n")
        while True:
            round_start=time.time()
            sensors['Epoch'] = round_start
            # System UTC epoch time
            lr="%d\t" % round_start
 
            # GPS data 
            logging.debug("Retrieving: GPS data")
            sys.stdout.write("\nGPSTime: %s GPSfix: %d Alt: %.1f m Lat: %f Lon: %f " % (gpsd.utc, gpsd.fix.mode, gpsd.fix.altitude, gpsd.fix.latitude, gpsd.fix.longitude))
            #sys.stdout.write("GPSfix: %d " % (gpsd.fix.mode))
            lr = lr + ("%s\t%d\t%.1f\t%f\t%f\t" % (gpsd.utc, gpsd.fix.mode, gpsd.fix.altitude, gpsd.fix.latitude, gpsd.fix.longitude))
            
            sensors['GPS_Time']=gpsd.utc
            sensors['GPS_Fix']=gpsd.fix.mode
            sensors['GPS_Alt']=gpsd.fix.altitude
            sensors['GPS_Lat']=gpsd.fix.latitude
            sensors['GPS_Lon']=gpsd.fix.longitude

            # GSM module data
            sys.stdout.write("GSM: %d %s Cell: %s " % (gsmpart.signalStrength,gsmpart.networkName, gsmpart.cellInfo))
            lr = lr + ("%d\t%s\t" % (gsmpart.signalStrength, gsmpart.cellInfo))
            sensors['GSM_Signal'] = gsmpart.signalStrength
	    sensors['GSM_CellInfo'] = gsmpart.cellInfo

            # CPU Temperature
            logging.debug("Retrieving: CPU thermal sensor data")
            with open("/sys/class/thermal/thermal_zone0/temp") as cputempf:
                cputemp=cputempf.readline()
                cputempf.close()
                cputemp=float(cputemp.rstrip())/1000.0
                sys.stdout.write("CPUTemp %.1f C " % cputemp)
                lr=lr+"%.2f\t" % (cputemp)
                sensors['CPU_Temp'] = cputemp

            # Altimet
            logging.debug("Retrieving: Altimet temperature and pressure data")
            altimet.route()
            (t1, p1) = altimet.get_tp()
            if (p1 == 0):
              logging.error('Altimet malfunction - no data from pressure indicator.')
            sys.stdout.write("AltiTemp: %.2f C Press: %d " % (t1, p1))
            lr=lr+("%.3f\t%d\t" % (t1, p1))
            sensors['Altimet_Temp'] = t1
            sensors['Altimet_Press'] = p1

            # SHT sensor	
            logging.debug("Retrieving: SHT sensor data")
            sht_sensor.route()	    	
            temperature = sht_sensor.get_temp()
            humidity = sht_sensor.get_hum()
            sys.stdout.write("SHTTemp: %.2f C Humid: %.1f " % (temperature, humidity))
            lr=lr+("%.2f\t%.1f\t" % (temperature, humidity))
            sensors['SHT_Temp'] = temperature
            sensors['SHT_Hum'] = humidity

            # Battery sensors
            logging.debug("Retrieving: Battery sensor data")
            guage.route()
            sensors['Bat_Temp'] = guage.getTemp()
            sensors['Bat_RemCap'] = guage.getRemainingCapacity()
            sensors['Bat_FullChargeCapacity'] = guage.FullChargeCapacity()
            sensors['Bat_V'] = guage.Voltage()
            sensors['Bat_AvgI'] = guage.AverageCurrent()
            sensors['Bat_Charge'] = guage.StateOfCharge()

            sys.stdout.write("BatTemp: %.2f C RemCap: %d mAh FullCap: %d mAh U: %d mV I: %d mA Charge: %.2f %%\n" % (guage.getTemp(), guage.getRemainingCapacity(), guage.FullChargeCapacity(), guage.Voltage(), guage.AverageCurrent(), guage.StateOfCharge()))
            #print "BatTemp: ", guage.getTemp(), "degC, RemainCapacity =", guage.getRemainingCapacity(), "mAh, cap =", guage.FullChargeCapacity(), "mAh, U =", guage.Voltage(), "mV, I =", guage.AverageCurrent(), "mA, charge =", guage.StateOfCharge(), "%"
            lr=lr + ("%.2f\t%d\t%d\t%d\t%d\t%.2f\n" % (guage.getTemp(), guage.getRemainingCapacity(), guage.FullChargeCapacity(), guage.Voltage(), guage.AverageCurrent(), guage.StateOfCharge()))

            sensors['Ready']=True
            logging.debug("Writing to file")
            f.write(lr) 
	    f.flush()
            sys.stdout.flush()
            round_timeleft = round_beat + round_start - time.time()
            if (round_timeleft > 0):
              time.sleep(round_timeleft)

except (KeyboardInterrupt, SystemExit):
    logging.error("Exiting:")
    #f.write("\r\n")
    f.close()
    if gsmpart.running:
      gsmpart.running = False
      #gsmpart.join()
      logging.info("GSM thread asked to shut down.")
    if webcam_enabled:
      webcam.running = False
      #webcam.join() # wait for WebCam thread
      logging.info("Webcam thread asked to shut down.")
    if gpsp.running:
      gpsp.running = False
      #gpsp.join()   # wait for GPS poller thread
      logging.info("GPS thread asked to shut down.")
    sys.exit(0)

