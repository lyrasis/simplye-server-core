import base64
import urlparse
import time
import hmac
import hashlib
import os
import requests

class ThreeMAPI(object):

    # TODO: %a and %b are localized per system, but 3M requires
    # English.
    AUTH_TIME_FORMAT = "%a, %d %b %Y %H:%M:%S GMT"
    ARGUMENT_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
    AUTHORIZATION_FORMAT = "3MCLAUTH %s:%s"

    DATETIME_HEADER = "3mcl-Datetime"
    AUTHORIZATION_HEADER = "3mcl-Authorization"
    VERSION_HEADER = "3mcl-APIVersion"

    def __init__(self, _db, account_id=None, library_id=None, account_key=None,
                 base_url = "http://cloudlibraryapi.3m.com/",
                 version="1.0"):
        self._db = _db
        self.version = version
        self.library_id = library_id or os.environ['THREEM_LIBRARY_ID']
        self.account_id = account_id or os.environ['THREEM_ACCOUNT_ID']
        self.account_key = account_key or os.environ['THREEM_ACCOUNT_KEY']
        self.base_url = base_url
        self.source = DataSource.lookup(self._db, DataSource.THREEM)

    def now(self):
        """Return the current GMT time in the format 3M expects."""
        return time.strftime(self.AUTH_TIME_FORMAT, time.gmtime())

    def sign(self, method, headers, path):
        """Add appropriate headers to a request."""
        authorization, now = self.authorization(method, path)
        headers[self.DATETIME_HEADER] = now
        headers[self.VERSION_HEADER] = self.version
        headers[self.AUTHORIZATION_HEADER] = authorization

    def authorization(self, method, path):
        signature, now = self.signature(method, path)
        auth = self.AUTHORIZATION_FORMAT % (self.account_id, signature)
        return auth, now

    def signature(self, method, path):
        now = self.now()
        signature_components = [now, method, path]
        signature_string = "\n".join(signature_components)
        digest = hmac.new(self.account_key, msg=signature_string,
                    digestmod=hashlib.sha256).digest()
        signature = base64.b64encode(digest)
        return signature, now

    def request(self, path, body=None, method="GET", identifier=None,
                cache_result=True):
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/cirrus"):
            path = "/cirrus/library/%s%s" % (self.library_id, path)
        url = urlparse.urljoin(self.base_url, path)
        headers = {}
        self.sign(method, headers, path)

        if cache_result and method=='GET':
            representation, cached = Representation.get(
                self._db, url, extra_request_headers=headers,
                data_source=self.source, identifier=identifier,
                do_get=Representation.http_get_no_timeout)
            content = representation.content
        else:
            response = requests.request(
                method, url, data=body, headers=headers)
            content = response.text
        return content

      