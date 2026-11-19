import importlib, json, os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class LegacyKeyTest(unittest.TestCase):
    def setUp(self):
        os.environ["ADMIN_KEY"] = "clave-entorno"
        import app as appmod
        self.app = importlib.reload(appmod)
        salt = "abc"
        self.app.ADMIN_STATE_FILE.write_text(json.dumps({"salt": salt, "hash": self.app._admin_hash("vieja123", salt)}))
        self.client = self.app.app.test_client()

    def tearDown(self):
        try:
            self.app.ADMIN_STATE_FILE.unlink()
        except FileNotFoundError:
            pass

    def test_env_key(self):
        self.assertEqual(self.client.post("/admin", data={"key": "clave-entorno"}).status_code, 200)

    def test_legacy_key(self):
        self.assertEqual(self.client.post("/admin", data={"key": "vieja123"}).status_code, 200)

    def test_wrong_key(self):
        self.assertEqual(self.client.post("/admin", data={"key": "otra"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
