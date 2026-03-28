import pytest, math
from cerberus.anatomy.kinematics import (
    DigitalAnatomy, forward_kinematics, support_polygon,
    stability_margin, EnergyModel, JointState, FootPosition
)
from cerberus.bridge.go2_bridge import RobotState

@pytest.mark.asyncio
async def test_anatomy_update():
    anatomy = DigitalAnatomy()
    state = RobotState(
        joint_positions=[0.0]*12, joint_velocities=[0.0]*12,
        joint_torques=[5.0]*12, foot_force=[20.0]*4,
    )
    await anatomy.update(state)
    d = anatomy.status()
    assert "joints" in d and len(d["joints"]) == 12
    assert "com" in d and "energy" in d

def test_forward_kinematics():
    x, y, z = forward_kinematics(0.0, 0.5, -1.0, side="L")
    assert isinstance(x, float)
    assert z < 0   # foot should be below hip

def test_support_polygon_4_feet():
    feet = [FootPosition(leg=l, x=x, y=y, z=-0.3, contact=True, force=20.0)
            for l, x, y in [("FL",0.2,0.1),("FR",0.2,-0.1),("RL",-0.2,0.1),("RR",-0.2,-0.1)]]
    poly = support_polygon(feet)
    assert len(poly) >= 3

def test_stability_margin():
    poly = [(0.2,0.1),(0.2,-0.1),(-0.2,-0.1),(-0.2,0.1)]
    margin = stability_margin((0.0, 0.0, 0.27), poly)
    assert margin > 0

def test_energy_model():
    em = EnergyModel()
    joints = [JointState(name=f"j{i}", torque=10.0, velocity=0.5) for i in range(12)]
    em.update(joints, dt=1.0)
    d = em.to_dict()
    assert d["total_power_w"] > 0
    assert d["estimated_runtime_min"] > 0

def test_joint_at_limit():
    j = JointState(name="FL_knee", position=4.15)   # near knee max 4.189
    assert j.at_limit is True
