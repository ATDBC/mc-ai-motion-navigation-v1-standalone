"""Respect the existing three-degree step after native binary32 addition."""
import math
import struct


def native_safe_gaze(own,look):
    def f32(value):return struct.unpack('!f',struct.pack('!f',value))[0]
    before=(f32(own.yaw),f32(own.pitch))
    delta=(look.yaw_delta_degrees,look.pitch_delta_degrees)
    after=(f32(before[0]+f32(delta[0])),max(-90.,min(90.,f32(before[1]+f32(delta[1])))))
    yaw=(after[0]-before[0]+180.)%360.-180.
    if math.hypot(yaw,after[1]-before[1])<=3.:return None
    # Two native ULPs cover endpoint rounding and decimal-to-native input
    # recovery. Usually this reduces a three-degree request by a few millionths.
    magnitude=max(1.,*(abs(v) for v in (*before,*after)))
    ulp=math.ldexp(1.,math.frexp(magnitude)[1]-24)
    scale=max(0.,3.-2*ulp)/max(3.,math.hypot(*delta))
    return own.yaw+delta[0]*scale,own.pitch+delta[1]*scale
