from subprocess import Popen
import unittest
import urllib.request


class TestServer(unittest.TestCase):

    def setUp(self):
      self.server = Popen(["gunicorn", "conf.wsgi", "--config", "conf/gunicorn.py"])

    def tearDown(self):
      self.server.kill()

    def test_server_accepts_http(self):
      def is_http_accepted():
          try:
            urllib.request.urlopen('http://localhost:8000', timeout=1)
            return True
          except urllib.request.URLError as e:
            return 'nodename nor servname provided, or not known' not in str(e.reason)
      self.assertTrue(is_http_accepted())
