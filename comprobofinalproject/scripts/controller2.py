#!/usr/bin/env python

import roslib
# roslib.load_manifest('my_package')
import sys
import rospy
import cv2
from std_msgs.msg import String
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
from sensor_msgs.msg import CompressedImage
from geometry_msgs.msg import PoseStamped, PoseWithCovarianceStamped, PoseArray, Pose, Point, Quaternion
from std_msgs.msg import Header, String
from nav_msgs.msg import Odometry
from comprobofinalproject.msg import Intersection

from comprobofinalproject.srv import *

from geometry_msgs.msg import Twist, Vector3
import numpy as np
import math
import random

import copy

from PIDcontroller import PID

#use colorCalibration.py to find the correct color range for the red line
#connect to neato --> run this and StopSignFinder --> turn robot to on

class controller:
    def __init__(self, verbose = False):
        rospy.init_node('comprobofinalproject', anonymous=True)
        cv2.namedWindow('image')

        # if true, we print what is going on
        self.verbose = verbose

        # most recent raw CV image
        self.cv_image = None
        self.newImage = False
        
        self.bridge = CvBridge()

        self.createTrackbars()

        self.speed = 1
        
        #subscribe tocamera images
        self.image_sub = rospy.Subscriber("camera/image_raw", Image, self.recieveImage)

        #subscribe to intersection
        self.inter_sub = rospy.Subscriber("/intersection",Intersection, self.intersectionCallback)

        

        #subscribe to odometry and initialize constants
        rospy.Subscriber('odom',Odometry,self.odometryCb)
        self.newOdom = False
        self.xPosition = None
        self.yPosition = None

        #publisher to call for a new visual map to be made
        self.visPub = rospy.Publisher("/mapVisual", String, queue_size=10)

        #initalize publisher that let's nodes know what the current task is ie (Mapping, RandomlyMoving, PathPlanning)
        self.taskPub = rospy.Publisher('/task', String, queue_size=10)
        self.taskPub.publish("Random")

        #set up publisher to send commands to the robot
        self.velPub = rospy.Publisher('/cmd_vel', Twist, queue_size=10)

        #subscribe to the sign found topic and intialize constants
        self.sign_sub = rospy.Subscriber("/sign_found", String, self.signFound)
        self.signDetected = False
        self.intersectionDetected = False
        self.signTimer = 0

        #initialize the PID controller and se the mode to line following
        self.mode = "lineFollowing"
        self.initializeLineFollowPID()

        #ensure service getTurn in running
        rospy.wait_for_service('getTurn')

        cv2.waitKey(3)

        self.dprint("Driver Initiated")

    def createTrackbars(self):
        #create on/off switch for robot, defaulted to off
        self.switchC = 'Controller \n0 : OFF \n1 : ON'
        cv2.createTrackbar(self.switchC, 'image',0,1,self.stop)
        cv2.setTrackbarPos(self.switchC,'image',0)

        self.switchM = 'sendCommand \n 0 : OFF \n1 : ON'
        cv2.createTrackbar(self.switchM, 'image',0,1,self.stop)
        cv2.setTrackbarPos(self.switchM,'image',1)

        self.switchTask = 'Task \n 0 : Random \n1 : BuildMap \n2 : GoToPoint'
        cv2.createTrackbar(self.switchTask, 'image',0,2,self.setTask)
        cv2.setTrackbarPos(self.switchTask,'image',0)

        cv2.createTrackbar('speed','image',0,200,nothing)
        cv2.setTrackbarPos('speed','image',10)

        cv2.createTrackbar('pidP','image',0,8000,nothing)
        cv2.setTrackbarPos('pidP','image',130)

        cv2.createTrackbar('pidI','image',0,400,nothing)
        cv2.setTrackbarPos('pidI','image',4)

        cv2.createTrackbar('pidD','image',0,4000,nothing)
        cv2.setTrackbarPos('pidD','image',20)

        cv2.createTrackbar('lowH','image',0,255,nothing)
        cv2.setTrackbarPos('lowH','image',0)
        cv2.createTrackbar('lowS','image',0,255,nothing)
        cv2.setTrackbarPos('lowS','image',156)
        cv2.createTrackbar('lowV','image',0,255,nothing)
        cv2.setTrackbarPos('lowV','image',87)
        cv2.createTrackbar('highH','image',0,255,nothing)
        cv2.setTrackbarPos('highH','image',255)
        cv2.createTrackbar('highS','image',0,255,nothing)
        cv2.setTrackbarPos('highS','image',255)
        cv2.createTrackbar('highV','image',0,255,nothing)
        cv2.setTrackbarPos('highV','image',169)

    def mainloop(self):
        if cv2.getTrackbarPos(self.switchC,'image') == 1:
            #store temporary versions of variables in case they change mid loop
            self.newOdomTemp = self.newOdom
            self.newImageTemp = self.newImage
            self.intersectionDetectedTemp = self.intersectionDetected
            self.signDetectedTemp = self.signDetected
            self.newOdom = False
            self.newImage = False
            self.intersectionDetected = False
            self.signDetected = False
            self.xPositionTemp = self.xPosition
            self.yPositionTemp = self.yPosition
            self.cv_imageTemp = copy.copy(self.cv_image)
            
            #decrement signTimer each loop through
            self.signTimer -= 1
            print self.signTimer


            if self.signTimer <= 0:
                #reset the sign parameters
                self.speed = 1
                self.signDetected = False

            if self.newImageTemp:
                self.findLine()

            if self.intersectionDetectedTemp:
                self.mode = "driveToIntersection"
                self.driveToIntersection()
                print "Now driving toward intersection"
            if self.mode == "driveToIntersection" and self.newOdomTemp:
                self.driveToIntersection()
            if self.mode == "rotateAtIntersection" and self.newOdomTemp:
                self.rotateAtIntersection()
            if self.newImageTemp and self.mode == "lineFollowing":
                self.lineFollow()

    def signFound(self,msg):
        #["yield", "stopinvert", "police", "speedlimit", "oneway"] 
        print "Sign Found"
        if msg.data == "stopinvert":
            #stop sign found
            self.speed = 0
            self.signTimer = 30
            self.signDetected = True 
            print "Stop!!!"
        elif msg.data == "police":
            #police spotted
            self.speed = .5
            self.signTimer = 60
            self.signDetected = True 
            print "Police!!!"
        elif msg.data == "speedlimit":
            #speed limit sign found
            self.speed = 1.2
            self.signTimer = 60
            self.signDetected = True 
            print "Speed up!!!"

    def driveToIntersection(self):


        distFromIntersection = self.euclidDistance(self.xPosition,self.yPosition,self.intersection.x,self.intersection.y)
        print "distFromIntersection: " + str(distFromIntersection)

        if abs(distFromIntersection) < (.025):
            #if distance from intersection less than 2.5 cm
            self.sendCommand(0, 0)
            self.mode = "rotateAtIntersection"

            #find the signed difference in current heading and desired heading
            self.angDif = math.atan2(math.sin(self.chosenExit-self.zAngle), math.cos(self.chosenExit-self.zAngle))
            print "now rotating at intersection"
        else:
            #find angle in odom frame of the line from current position to intersection position
            angleToGoal = math.atan2(self.intersection.y-self.yPosition,self.intersection.x - self.xPosition)

            #find the signed difference in current heading and desired heading
            angDif = math.atan2(math.sin(angleToGoal-self.zAngle), math.cos(angleToGoal-self.zAngle))
            print "AngDif: " + str(angDif)

            #use the signed angle difference to decide rotation direction
            turn = math.copysign(.10, angDif)


            if abs(angDif) > math.pi/72:
                #if the magnitude of the angular difference between the current and desired heading is greater than pi/72, ocrrect course
                self.sendCommand(.10, turn)
            else:
                #facing the intersection, drive forward to it
                self.sendCommand(.10, 0)

    def rotateAtIntersection(self):
        #find the signed difference in current heading and desired heading
        angDif = math.atan2(math.sin(self.chosenExit-self.zAngle), math.cos(self.chosenExit-self.zAngle))
        print "angDif: " + str(angDif)

        #can the robot see a line
        if self.averageLineIndex == None:
            lineInRange = False
        elif (self.averageLineIndex - 320) < 250:
            lineInRange = True
        else: 
            lineInRange = False


        if abs(angDif) < (math.pi/6) and lineInRange:
            #Chosen Exit found, initializing line following
            self.sendCommand(0, 0)
            self.mode = "lineFollowing"
            self.initializeLineFollowPID()
        else:
            #use original direction calculated in case the angle measurement is slightly off (allows the neato to keep looking past the calcualted angle)
            self.sendCommand(0, math.copysign(.20, self.angDif))

    def intersectionCallback(self,msg):
        print msg        
        try:
            #check if interesection is the same as the last one found
            if self.euclidDistance(self.intersection.x,self.intersection.y,msg.x,msg.y) < .05:
                #don't reaact to the same interesection found twice
                return
        except:
            #in case there is no previous intersection and thus self.intersection does not exist
            pass

        #set the intersection  to the found intersection
        self.intersection = msg

        #get what direction to turn
        getTurnServiceProxy = rospy.ServiceProxy('getTurn', intersectionFoundGetTurn)
        resp1 = getTurnServiceProxy(x = msg.x, y = msg.y, exits = msg.exits, exits_raw = msg.raw_exits, current_path_exit = msg.current_path_exit)
        self.chosenExit = resp1.exit_chosen

        self.intersectionDetected = True

        #calculate and display distance from intersection
        distTravelled = self.euclidDistance(self.xPosition,self.yPosition,self.intersection.x,self.intersection.y)
        print "distToIntersectionInitial: " + str(distTravelled)
        print "calculated Dist: " + str(self.intersection.distance)

        #tell map node to produce a new visual map
        self.visPub.publish("makeMap")

    def findLine(self):
        smallCopy = self.cv_imageTemp[350:478]

        hsv = cv2.cvtColor(smallCopy, cv2.COLOR_BGR2HSV)

        lowH = cv2.getTrackbarPos('lowH','image')
        lowS = cv2.getTrackbarPos('lowS','image')
        lowV = cv2.getTrackbarPos('lowV','image')
        highH = cv2.getTrackbarPos('highH','image')
        highS = cv2.getTrackbarPos('highS','image')
        highV = cv2.getTrackbarPos('highV','image')

        lower_red = np.array([lowH,lowS,lowV])
        upper_red = np.array([highH,highS,highV])

        mask = cv2.inRange(hsv, lower_red, upper_red)

        cv2.imshow('mask', mask)

        #sum all coplumns into 1 row
        driveRow = np.sum(mask,0)
        
        #initialize array of x indicies where the road exists
        num = []

        #fill array num with x indicies where the road exists
        for i in range(len(driveRow)):
            if driveRow[i] > 0:
                num.append(i+1)

        try:
            self.averageLineIndex = (float(sum(num))/len(num))
            #print "averageLineIndex: " + str(self.averageLineIndex)
        except:
            self.averageLineIndex = None
            #print "no line found"
            return

    def lineFollow(self):
        if self.averageLineIndex != None:
            #get and set PID control constants
            pidP100 = cv2.getTrackbarPos('pidP','image')
            pidI100 = cv2.getTrackbarPos('pidI','image')
            pidD100 = cv2.getTrackbarPos('pidD','image')

            pidP = float(pidP100)/100
            pidI = float(pidI100)/100
            pidD = float(pidD100)/100

            self.pid.setKp(pidP)
            self.pid.setKi(pidI)
            self.pid.setKd(pidD)

            #use the pid controller to determine the anglular velocity
            ang = self.pid.update(self.averageLineIndex)/1000

            speed = cv2.getTrackbarPos('speed','image')/100.0

            self.sendCommand(speed, ang)

    def initializeLineFollowPID(self):
        self.pid = PID(P=2.0, I=0.0, D=1.0, Derivator=0, Integrator=0, Integrator_max=500, Integrator_min=-500)
        self.pid.setPoint(float(320))

    #calculate distance between 2 points
    def euclidDistance(self,x1,y1,x2,y2):
        return math.hypot(x2 - x1, y2 - y1)

    #function that stops the robot is 0 is passed in, primary use is call back from stop switch
    def stop(self, x):
        if x == 0:
            self.sendCommand(0,0)

    def setTask(self,x):
        if x == 0:
            task = "Random"
        elif x == 1:
            task = "Map"
        elif x == 2:
            task = "Path"
        self.taskPub.publish(task)
    
    #odometry callback
    def odometryCb(self,odom):
        self.xPosition = odom.pose.pose.position.x
        self.yPosition = odom.pose.pose.position.y
        self.zAngle = math.atan2(2* (odom.pose.pose.orientation.z * odom.pose.pose.orientation.w),1 - 2 * ((odom.pose.pose.orientation.y)**2 + (odom.pose.pose.orientation.z)**2))
        self.newOdom = True
        
    # callback when image recieved
    def recieveImage(self,raw_image):
        self.dprint("Image Recieved")

        try:
            self.cv_image = self.bridge.imgmsg_to_cv2(raw_image, "bgr8")
            self.newImage = True
        except CvBridgeError, e:
            print e
                   
        #display image recieved
        #cv2.imshow('Video1', self.cv_image)

        cv2.waitKey(3)

    #send movement command to robot
    def sendCommand(self, lin, ang):
        #apply sign information
        lin = self.speed*lin

        #send twist command if the motors are set to on
        if cv2.getTrackbarPos(self.switchM,'image') == 1:
            twist = Twist()
            twist.linear.x = lin; twist.linear.y = 0; twist.linear.z = 0
            twist.angular.x = 0; twist.angular.y = 0; twist.angular.z = ang
            self.velPub.publish(twist)

    #function that makes print statements switchable
    def dprint(self, print_message):
        if self.verbose:
            print print_message

def nothing(x):
    pass

def main(args):
    # initialize driver
    ic = controller(False)

    #set ROS refresh rate
    r = rospy.Rate(30)

    #keep program running until shutdown
    while not(rospy.is_shutdown()):
        ic.mainloop()
        r.sleep()

    #close all windows on exit
    cv2.destroyAllWindows()

if __name__ == '__main__':
    main(sys.argv)
