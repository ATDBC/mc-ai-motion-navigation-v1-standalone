import unittest
from dataclasses import replace

from mc2p.contracts.action_v1 import LookV1
from mc2p.skills.follow_tracking import project_playground_view
from mc2p.skills.navigation_look import ObservationGate
from tests.test_navigation_motion import floor_snapshot, NOW


def view(sequence=0, now=NOW, **kwargs):
    return project_playground_view(floor_snapshot(sequence,now,**kwargs),now,'controller-test')


class NavigationLookTests(unittest.TestCase):
    def test_caller_can_request_precise_deadband_without_changing_navigation_default(self):
        initial = view(yaw=0, pitch=0)
        self.assertEqual(ObservationGate().request(initial, 3, 2, NOW), LookV1())
        precise = ObservationGate().request(
            initial, 3, 2, NOW, yaw_tolerance=0.5, pitch_tolerance=0.5,
        )
        self.assertEqual((precise.yaw_delta_degrees, precise.pitch_delta_degrees), (3, 2))

    def test_request_needs_selected_feedback_and_actual_new_heading(self):
        gate=ObservationGate(); initial=view(yaw=0,pitch=30)
        look=gate.request(initial,15,30,NOW)
        self.assertEqual(look.yaw_delta_degrees,15)
        self.assertFalse(gate.confirmed(initial,NOW))
        post=view(1,NOW+50_000_000,yaw=15,pitch=30)
        self.assertFalse(gate.confirmed(post,NOW+50_000_000))
        gate.feedback(True,post,NOW+50_000_000)
        self.assertTrue(gate.confirmed(post,NOW+50_000_000))
        self.assertFalse(gate.confirmed(post,NOW+600_000_000))

    def test_preempted_same_frame_wrong_heading_and_clock_never_confirm(self):
        initial=view(yaw=0,pitch=30)
        for selected, post in ((False,view(1,NOW+50_000_000,yaw=15)),
                              (True,initial),(True,view(1,NOW+50_000_000,yaw=0)),
                              (True,replace(view(1,NOW+50_000_000,yaw=15),
                                 base=replace(view(1,NOW+50_000_000,yaw=15).base,client_clock_id='other')))):
            gate=ObservationGate(); gate.request(initial,15,30,NOW)
            gate.feedback(selected,post,NOW+50_000_000)
            self.assertFalse(gate.confirmed(post,NOW+50_000_000))

    def test_clear_and_new_request_cannot_reuse_earlier_confirmation(self):
        gate=ObservationGate(); gate.request(view(),15,30,NOW)
        post=view(1,NOW+50_000_000,yaw=15)
        gate.feedback(True,post,NOW+50_000_000)
        gate.request(post,30,30,NOW+50_000_000)
        self.assertFalse(gate.confirmed(post,NOW+50_000_000))
        gate.clear()
        self.assertFalse(gate.confirmed(post,NOW+50_000_000))
