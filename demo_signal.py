import math
def simulated_imu(t,rng):
    # Demonstration only: illustrative raw scales, not device-calibrated units.
    phase=t%12
    angle=0.; speed=0.
    if 3<=phase<7:
        u=phase-3; angle=.8*math.sin(math.pi*u); speed=.8*math.pi*math.cos(math.pi*u)
    elif 7<=phase<9:
        u=phase-7; angle=.4*math.sin(4*math.pi*u); speed=1.6*math.pi*math.cos(4*math.pi*u)
    burst=1800*math.exp(-((phase-9.5)/.08)**2)
    return [int(8192*math.sin(angle)+rng.uniform(-15,15)),int(burst+rng.uniform(-15,15)),int(-8192*math.cos(angle)+rng.uniform(-15,15)),int(rng.uniform(-10,10)),int(speed*180/math.pi/.06104+rng.uniform(-10,10)),int(burst*.5+rng.uniform(-10,10))]

