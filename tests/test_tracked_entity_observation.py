"""Native projection of one already registered living entity."""
import unittest
from tests import test_visible_equipment_projection as native


class TrackedEntityObservationTests(unittest.TestCase):
    def test_registered_living_entity_projects_exact_movement_and_health(self):
        native.VisibleEquipmentProjectionTests()._run_java_harness(
            "TrackedEntityObservationTest", "TRACKED_ENTITY_OBSERVATION_OK")


if __name__ == "__main__":
    unittest.main()
