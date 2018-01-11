# Anymail test utils
import sys
from distutils.util import strtobool

import os
import re
import warnings
from base64 import b64decode
from contextlib import contextmanager

import six
from django.test import Client


def envbool(var, default=False):
    """Returns value of environment variable var as a bool, or default if not set.

    Converts `'true'` to `True`, and `'false'` to `False`.
    See :func:`~distutils.util.strtobool` for full list of allowable values.
    """
    val = os.getenv(var, None)
    if val is None:
        return default
    else:
        return strtobool(val)


# RUN_LIVE_TESTS: whether to run live API integration tests.
# True by default, except in CONTINUOUS_INTEGRATION job.
# (See comments and overrides in .travis.yml.)
RUN_LIVE_TESTS = envbool('RUN_LIVE_TESTS', default=not envbool('CONTINUOUS_INTEGRATION'))


def decode_att(att):
    """Returns the original data from base64-encoded attachment content"""
    return b64decode(att.encode('ascii'))


def rfc822_unfold(text):
    # "Unfolding is accomplished by simply removing any CRLF that is immediately followed by WSP"
    # (WSP is space or tab, and per email.parser semantics, we allow CRLF, CR, or LF endings)
    return re.sub(r'(\r\n|\r|\n)(?=[ \t])', "", text)


#
# Sample files for testing (in ./test_files subdir)
#

TEST_FILES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test_files')

SAMPLE_IMAGE_FILENAME = "sample_image.png"
SAMPLE_EMAIL_FILENAME = "sample_email.txt"


def test_file_path(filename):
    """Returns path to a test file"""
    return os.path.join(TEST_FILES_DIR, filename)


def test_file_content(filename):
    """Returns contents (bytes) of a test file"""
    path = test_file_path(filename)
    with open(path, "rb") as f:
        return f.read()


def sample_image_path(filename=SAMPLE_IMAGE_FILENAME):
    """Returns path to an actual image file in the tests directory"""
    return test_file_path(filename)


def sample_image_content(filename=SAMPLE_IMAGE_FILENAME):
    """Returns contents of an actual image file from the tests directory"""
    return test_file_content(filename)


def sample_email_path(filename=SAMPLE_EMAIL_FILENAME):
    """Returns path to an email file (e.g., for forwarding as an attachment)"""
    return test_file_path(filename)


def sample_email_content(filename=SAMPLE_EMAIL_FILENAME):
    """Returns bytes contents of an email file (e.g., for forwarding as an attachment)"""
    return test_file_content(filename)


#
# TestCase helpers
#

# noinspection PyUnresolvedReferences
class AnymailTestMixin:
    """Helpful additional methods for Anymail tests"""

    def assertWarns(self, expected_warning, msg=None):
        # We only support the context-manager version
        try:
            return super(AnymailTestMixin, self).assertWarns(expected_warning, msg=msg)
        except TypeError:
            # Python 2.x: use our backported assertWarns
            return _AssertWarnsContext(expected_warning, self, msg=msg)

    def assertWarnsRegex(self, expected_warning, expected_regex, msg=None):
        # We only support the context-manager version
        try:
            return super(AnymailTestMixin, self).assertWarnsRegex(expected_warning, expected_regex, msg=msg)
        except TypeError:
            # Python 2.x: use our backported assertWarns
            return _AssertWarnsContext(expected_warning, self, expected_regex=expected_regex, msg=msg)

    @contextmanager
    def assertDoesNotWarn(self, disallowed_warning=Warning):
        """Makes test error (rather than fail) if disallowed_warning occurs.

        Note: you probably want to be more specific than the default
        disallowed_warning=Warning, which errors for any warning
        (including DeprecationWarnings).
        """
        try:
            warnings.simplefilter("error", disallowed_warning)
            yield
        finally:
            warnings.resetwarnings()

    def assertCountEqual(self, *args, **kwargs):
        try:
            return super(AnymailTestMixin, self).assertCountEqual(*args, **kwargs)
        except TypeError:
            return self.assertItemsEqual(*args, **kwargs)  # Python 2

    def assertRaisesRegex(self, *args, **kwargs):
        try:
            return super(AnymailTestMixin, self).assertRaisesRegex(*args, **kwargs)
        except TypeError:
            return self.assertRaisesRegexp(*args, **kwargs)  # Python 2

    def assertRegex(self, *args, **kwargs):
        try:
            return super(AnymailTestMixin, self).assertRegex(*args, **kwargs)
        except TypeError:
            return self.assertRegexpMatches(*args, **kwargs)  # Python 2

    def assertEqualIgnoringHeaderFolding(self, first, second, msg=None):
        # Unfold (per RFC-8222) all text first and second, then compare result.
        # Useful for message/rfc822 attachment tests, where various Python email
        # versions handled folding slightly differently.
        # (Technically, this is unfolding both headers and (incorrectly) bodies,
        # but that doesn't really affect the tests.)
        if isinstance(first, six.binary_type) and isinstance(second, six.binary_type):
            first = first.decode('utf-8')
            second = second.decode('utf-8')
        first = rfc822_unfold(first)
        second = rfc822_unfold(second)
        self.assertEqual(first, second, msg)


# Backported from python 3.5
class _AssertWarnsContext(object):
    """A context manager used to implement TestCase.assertWarns* methods."""

    def __init__(self, expected, test_case, expected_regex=None, msg=None):
        self.test_case = test_case
        self.expected = expected
        self.test_case = test_case
        if expected_regex is not None:
            expected_regex = re.compile(expected_regex)
        self.expected_regex = expected_regex
        self.msg = msg

    def _raiseFailure(self, standardMsg):
        # msg = self.test_case._formatMessage(self.msg, standardMsg)
        msg = self.msg or standardMsg
        raise self.test_case.failureException(msg)

    def __enter__(self):
        # The __warningregistry__'s need to be in a pristine state for tests
        # to work properly.
        for v in sys.modules.values():
            if getattr(v, '__warningregistry__', None):
                v.__warningregistry__ = {}
        self.warnings_manager = warnings.catch_warnings(record=True)
        self.warnings = self.warnings_manager.__enter__()
        warnings.simplefilter("always", self.expected)
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.warnings_manager.__exit__(exc_type, exc_value, tb)
        if exc_type is not None:
            # let unexpected exceptions pass through
            return
        try:
            exc_name = self.expected.__name__
        except AttributeError:
            exc_name = str(self.expected)
        first_matching = None
        for m in self.warnings:
            w = m.message
            if not isinstance(w, self.expected):
                continue
            if first_matching is None:
                first_matching = w
            if self.expected_regex is not None and not self.expected_regex.search(str(w)):
                continue
            # store warning for later retrieval
            self.warning = w
            self.filename = m.filename
            self.lineno = m.lineno
            return
        # Now we simply try to choose a helpful failure message
        if first_matching is not None:
            self._raiseFailure('"{}" does not match "{}"'.format(
                     self.expected_regex.pattern, str(first_matching)))
        self._raiseFailure("{} not triggered".format(exc_name))


class ClientWithCsrfChecks(Client):
    """Django test Client that enforces CSRF checks

    https://docs.djangoproject.com/en/1.9/ref/csrf/#testing
    """

    def __init__(self, **defaults):
        super(ClientWithCsrfChecks, self).__init__(
            enforce_csrf_checks=True, **defaults)
