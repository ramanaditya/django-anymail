import re
from email.header import Header
from email.mime.base import MIMEBase

from django.core.mail import BadHeaderError

from .base import AnymailBaseBackend, BasePayload
from .._version import __version__
from ..exceptions import AnymailAPIError, AnymailImproperlyInstalled
from ..message import AnymailRecipientStatus
from ..utils import get_anymail_setting

try:
    import boto3
    from botocore.client import Config
    from botocore.exceptions import BotoCoreError, ClientError, ConnectionError
except ImportError:
    raise AnymailImproperlyInstalled(missing_package='boto3', backend='amazon_ses')


# boto3 has several root exception classes; this is meant to cover all of them
BOTO_BASE_ERRORS = (BotoCoreError, ClientError, ConnectionError)


# Work around Python 2 bug in email.message.Message.to_string, where long headers
# containing commas or semicolons get an extra space inserted after every ',' or ';'
# not already followed by a space. https://bugs.python.org/issue25257
if Header("test,Python2,header,comma,bug", maxlinelen=20).encode() == "test,Python2,header,comma,bug":
    # no workaround needed
    HeaderBugWorkaround = None

    def add_header(message, name, val):
        message[name] = val

else:
    # workaround: custom Header subclass that won't consider ',' and ';' as folding candidates

    class HeaderBugWorkaround(Header):
        def encode(self, splitchars=' ', **kwargs):  # only split on spaces, rather than splitchars=';, '
            return Header.encode(self, splitchars, **kwargs)

    def add_header(message, name, val):
        # Must bypass Django's SafeMIMEMessage.__set_item__, because its call to
        # forbid_multi_line_headers converts the val back to a str, undoing this
        # workaround. That makes this code responsible for sanitizing val:
        if '\n' in val or '\r' in val:
            raise BadHeaderError("Header values can't contain newlines (got %r for header %r)" % (val, name))
        val = HeaderBugWorkaround(val, header_name=name)
        assert isinstance(message, MIMEBase)
        MIMEBase.__setitem__(message, name, val)


class EmailBackend(AnymailBaseBackend):
    """
    Amazon SES Email Backend (using boto3)
    """

    esp_name = "Amazon SES"

    def __init__(self, **kwargs):
        """Init options from Django settings"""
        super(EmailBackend, self).__init__(**kwargs)
        # AMAZON_SES_CLIENT_PARAMS is optional - boto3 can find credentials several other ways
        self.client_params = _get_anymail_boto3_client_params(kwargs=kwargs)
        self.configuration_set_name = get_anymail_setting("configuration_set_name", esp_name=self.esp_name,
                                                          kwargs=kwargs, allow_bare=False, default=None)
        self.client = None

    def open(self):
        if self.client:
            return False  # already exists
        try:
            self.client = boto3.client("ses", **self.client_params)
        except BOTO_BASE_ERRORS:
            if not self.fail_silently:
                raise

    def close(self):
        if self.client is None:
            return
        # self.client.close()  # boto3 doesn't currently seem to support (or require) this
        self.client = None

    def build_message_payload(self, message, defaults):
        return AmazonSESPayload(message, defaults, self)

    def post_to_esp(self, payload, message):
        params = payload.get_api_params()
        try:
            response = self.client.send_raw_email(**params)
        except BOTO_BASE_ERRORS as err:
            # ClientError has a response attr with parsed json error response (other errors don't)
            raise AnymailAPIError(str(err), backend=self, email_message=message, payload=payload,
                                  response=getattr(err, 'response', None), raised_from=err)
        return response

    def parse_recipient_status(self, response, payload, message):
        # response is the parsed (dict) JSON returned the API call
        try:
            message_id = response["MessageId"]
        except (KeyError, TypeError) as err:
            raise AnymailAPIError(
                "%s parsing Amazon SES send result %r" % (str(err), response),
                backend=self, email_message=message, payload=payload)

        recipient_status = AnymailRecipientStatus(message_id=message_id, status="queued")
        return {recipient.addr_spec: recipient_status for recipient in payload.all_recipients}


class AmazonSESPayload(BasePayload):
    def init_payload(self):
        self.all_recipients = []

        self.mime_message = self.message.message()
        if HeaderBugWorkaround and "Subject" in self.mime_message:
            # (message.message() will have already checked subject for BadHeaderError)
            self.mime_message.replace_header("Subject", HeaderBugWorkaround(self.message.subject))

        self.params = {}
        if self.backend.configuration_set_name is not None:
            self.params["ConfigurationSetName"] = self.backend.configuration_set_name

    def get_api_params(self):
        self.params["RawMessage"] = {
            # Note: "Destinations" is determined from message headers if not provided
            # "Destinations": [email.addr_spec for email in self.all_recipients],
            "Data": self.mime_message.as_string()
        }
        return self.params

    # Standard EmailMessage attrs...
    # These all get rolled into the RFC-5322 raw mime directly via EmailMessage.message()

    def _no_send_defaults(self, attr):
        # Anymail global send defaults don't work for standard attrs, because the
        # merged/computed value isn't forced back into the EmailMessage.
        if attr in self.defaults:
            self.unsupported_feature("Anymail send defaults for '%s' with Amazon SES" % attr)

    def set_from_email_list(self, emails):
        # Although Amazon SES will send messages with any From header, it can only parse Source
        # if the From header is a single email. Explicit Source avoids an "Illegal address" error:
        if len(emails) > 1:
            self.params["Source"] = emails[0].addr_spec
        # (else SES will look at the (single) address in the From header)

    def set_recipients(self, recipient_type, emails):
        self.all_recipients += emails
        # included in mime_message
        assert recipient_type in ("to", "cc", "bcc")
        self._no_send_defaults(recipient_type)

    def set_subject(self, subject):
        # included in mime_message
        self._no_send_defaults("subject")

    def set_reply_to(self, emails):
        # included in mime_message
        self._no_send_defaults("reply_to")

    def set_extra_headers(self, headers):
        # included in mime_message
        self._no_send_defaults("extra_headers")

    def set_text_body(self, body):
        # included in mime_message
        self._no_send_defaults("body")

    def set_html_body(self, body):
        # included in mime_message
        self._no_send_defaults("body")

    def set_alternatives(self, alternatives):
        # included in mime_message
        self._no_send_defaults("alternatives")

    def set_attachments(self, attachments):
        # included in mime_message
        self._no_send_defaults("attachments")

    # Anymail-specific payload construction
    def set_envelope_sender(self, email):
        self.params["Source"] = email.addr_spec

    def set_spoofed_to_header(self, header_to):
        # django.core.mail.EmailMessage.message() has already set
        #   self.mime_message["To"] = header_to
        # and performed any necessary header sanitization
        self.params["Destinations"] = [email.addr_spec for email in self.all_recipients]

    def set_metadata(self, metadata):
        # Amazon SES has two mechanisms for adding custom data to a message:
        # * Custom message headers are available to webhooks (SNS notifications),
        #   but not in CloudWatch metrics/dashboards or Kinesis Firehose streams.
        # * "Message Tags" are available to CloudWatch and Firehose, but not SNS.
        #   (Message Tags also allow *very* limited characters.)
        # (See "How do message tags work?" in https://aws.amazon.com/blogs/ses/introducing-sending-metrics/
        # and https://forums.aws.amazon.com/thread.jspa?messageID=782922.)
        #
        # Anymail metadata is useful in all these contexts, so use both mechanisms.
        # (Same logic applies to Anymail tags.)
        add_header(self.mime_message, "X-Metadata", self.serialize_json(metadata))
        self.params.setdefault("Tags", []).extend(
            {"Name": self._clean_tag(key), "Value": self._clean_tag(value)}
            for key, value in metadata.items())

    def set_tags(self, tags):
        # (See note about Amazon SES Message Tags and custom headers in set_metadata above)
        for tag in tags:
            add_header(self.mime_message, "X-Tag", tag)  # creates multiple X-Tag headers, one per tag
        cleaned_tags = "__".join(self._clean_tag(tag) for tag in tags)
        self.params.setdefault("Tags", []).append(
            {"Name": "Tags", "Value": cleaned_tags})

    def set_template_id(self, template_id):
        # TODO: implement send_templated_email (uses different payload format; can't support attachments, etc.)
        # https://docs.aws.amazon.com/ses/latest/DeveloperGuide/send-personalized-email-advanced.html
        self.unsupported_feature("template_id")

    def set_merge_data(self, merge_data):
        self.unsupported_feature("merge_data without template_id")

    def set_merge_global_data(self, merge_global_data):
        self.unsupported_feature("global_merge_data without template_id")

    # ESP-specific payload construction
    def set_esp_extra(self, extra):
        # e.g., ConfigurationSetName, FromArn, SourceArn, ReturnPathArn
        self.params.update(extra)

    @staticmethod
    def _clean_tag(s):
        """Return a version of str s transformed for use as an AWS `Tags` name or value.

        AWS Tags allow only a-z, A-Z, 0-9, hyphen and underscore characters. (No spaces.)

        This transformation:
        * Makes no changes to strings that are already valid AWS Tags
        * Converts each group of whitespace characters to a single underscore
        * Converts each group of other prohibited characters to a single hyphen

        The result is meant to be usefully readable, but the transformation is not reversable.
        """
        s = str(s)
        s = re.sub(r'\s+', '_', s)  # whitespace to single underscore
        s = re.sub(r'[^A-Za-z0-9_\-]+', '-', s)  # everything else to single hyphens
        return s


def _get_anymail_boto3_client_params(name="client_params", esp_name=EmailBackend.esp_name, kwargs=None):
    """Returns dict of params for boto3.client(), incorporating ANYMAIL["AMAZON_SES_CLIENT_PARAMS"] setting"""
    # (shared with ..webhooks.amazon_ses)
    client_params = get_anymail_setting(name, esp_name=esp_name, kwargs=kwargs, default={}).copy()
    config = Config(user_agent_extra="django-anymail/{version}-{esp}".format(
        esp=esp_name.lower().replace(" ", "-"), version=__version__))
    if "config" in client_params:
        # convert config dict to botocore.client.Config if needed
        client_params_config = client_params["config"]
        if not isinstance(client_params_config, Config):
            client_params_config = Config(**client_params_config)
        config = config.merge(client_params_config)
    client_params["config"] = config
    return client_params
