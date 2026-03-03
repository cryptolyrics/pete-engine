import hashlib
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]


class PreservationTests(unittest.TestCase):
    def _sha256(self, path: pathlib.Path) -> str:
        h = hashlib.sha256()
        h.update(path.read_bytes())
        return h.hexdigest()

    def test_pete_nba_pipeline_copy_matches_root(self):
        root_file = ROOT / "pete-nba-pipeline.py"
        project_file = ROOT / "projects" / "pete-dfs" / "scripts" / "pete-nba-pipeline.py"
        self.assertEqual(self._sha256(root_file), self._sha256(project_file))

    def test_pete_engine_copy_matches_root(self):
        root_file = ROOT / "PeteDFS_engine.py"
        project_file = ROOT / "projects" / "pete-dfs" / "scripts" / "PeteDFS_engine.py"
        self.assertEqual(self._sha256(root_file), self._sha256(project_file))

    def test_draftstars_copy_matches_root(self):
        root_file = ROOT / "draftstars_final.py"
        project_file = ROOT / "projects" / "pete-dfs" / "scripts" / "draftstars_final.py"
        self.assertEqual(self._sha256(root_file), self._sha256(project_file))


if __name__ == "__main__":
    unittest.main()
