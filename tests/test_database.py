import tempfile
import unittest
from pathlib import Path

from database import Database


class NavigationPersistenceTests(unittest.TestCase):
    def test_navigation_message_ids_are_saved_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(str(Path(directory) / "sync.db"))

            db.save_navigation(-100123, 77, tree_message_id=88)
            db.save_navigation(-100123, 77, graph_message_id=99)

            self.assertEqual(
                db.get_navigation(-100123),
                {"topic_id": 77, "tree_message_id": 88, "graph_message_id": 99},
            )

    def test_known_topics_and_explicit_mapping_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(str(Path(directory) / "sync.db"))
            db.observe_topic(-100123, 77, "Проекты")
            self.assertEqual(db.list_known_topics(-100123)[0]["folder_path"], None)

            db.save_topic(-100123, "Work", 77, "Проекты")
            self.assertEqual(db.get_topic(-100123, "Work"), 77)
            self.assertEqual(db.list_known_topics(-100123)[0]["folder_path"], "Work")

            db.save_topic(-100123, "Moved/Work", 77, "Проекты")
            self.assertIsNone(db.get_topic(-100123, "Work"))
            self.assertEqual(db.get_topic(-100123, "Moved/Work"), 77)


if __name__ == "__main__":
    unittest.main()
