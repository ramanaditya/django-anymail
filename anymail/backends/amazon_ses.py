from .base import AnymailBaseBackend, BasePayload
from ..exceptions import AnymailAPIError, AnymailImproperlyInstalled
from ..message import AnymailRecipientStatus
from ..utils import get_anymail_setting

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError, ConnectionError
except ImportError:
    raise AnymailImproperlyInstalled(missing_package='boto3', backend='amazon_ses')


# boto3 has several root exception classes; this is meant to cover all of them
BOTO_BASE_ERRORS = (BotoCoreError, ClientError, ConnectionError)


class EmailBackend(AnymailBaseBackend):
    """
    Amazon SES Email Backend (using boto3)
    """

    esp_name = "Amazon SES"

    def __init__(self, **kwargs):
        """Init options from Django settings"""
        super(EmailBackend, self).__init__(**kwargs)
        # AMAZON_SES_CLIENT_PARAMS is optional - boto3 can find credentials several other ways
        self.client_params = get_anymail_setting("client_params", esp_name=self.esp_name,
                                                 kwargs=kwargs, allow_bare=False, default={})
        # TODO: maybe add a setting for default configuration set?
        #       (otherwise must use "AMAZON_SES_ESP_EXTRA": {"ConfigurationSetName": "my-default-set"})
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
        # There's actually no (supported) close method for a boto3 client/session.
        # boto3 just relies on garbage collection (and we're probably using a shared session anyway).
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
                                  response=getattr(err, 'response', None))
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
        self.mime_message = self.message.message()
        self.params = {}
        self.all_recipients = []

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
        # Anymail metadata dict becomes individual SES name:value Tags
        # TODO: AWS Tags are very restrictive on values (e.g., no spaces or commas); should we offer optional encoding?
        self.params.setdefault("Tags", []).extend(
            {"Name": key, "Value": str(value)} for key, value in metadata.items())

    def set_tags(self, tags):
        # Anymail tags list becomes SES Tags["TagN"] values
        self.params.setdefault("Tags", []).extend(
            {"Name": "Tag%d" % n, "Value": tags[n]} for n in range(len(tags)))

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
