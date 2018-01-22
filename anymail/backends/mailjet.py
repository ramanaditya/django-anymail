from requests.structures import CaseInsensitiveDict

from ..exceptions import AnymailRequestsAPIError
from ..message import AnymailRecipientStatus
from ..utils import get_anymail_setting, parse_address_list

from .base_requests import AnymailRequestsBackend, RequestsPayload


class EmailBackend(AnymailRequestsBackend):
    """
    Mailjet API Email Backend
    """

    esp_name = "Mailjet"

    def __init__(self, **kwargs):
        """Init options from Django settings"""
        esp_name = self.esp_name
        self.api_key = get_anymail_setting('api_key', esp_name=esp_name, kwargs=kwargs, allow_bare=True)
        self.secret_key = get_anymail_setting('secret_key', esp_name=esp_name, kwargs=kwargs, allow_bare=True)
        api_url = get_anymail_setting('api_url', esp_name=esp_name, kwargs=kwargs,
                                      default="https://api.mailjet.com/v3.1/")
        if not api_url.endswith("/"):
            api_url += "/"
        super(EmailBackend, self).__init__(api_url, **kwargs)

    def build_message_payload(self, message, defaults):
        return MailjetPayload(message, defaults, self)

    def raise_for_status(self, response, payload, message):
        if 400 <= response.status_code <= 499:
            # Mailjet uses 4xx status codes for partial failure in batch send;
            # we'll determine how to handle below in parse_recipient_status.
            return
        super(EmailBackend, self).raise_for_status(response, payload, message)

    def parse_recipient_status(self, response, payload, message):
        parsed_response = self.deserialize_json_response(response, payload, message)

        # Global error? (no messages sent)
        if "ErrorCode" in parsed_response:
            raise AnymailRequestsAPIError(email_message=message, payload=payload, response=response, backend=self)

        recipient_status = {}
        try:
            for result in parsed_response["Messages"]:
                status = 'sent' if result["Status"] == 'success' else 'failed'  # Status is 'success' or 'error'
                recipients = result.get("To", []) + result.get("Cc", []) + result.get("Bcc", [])
                for recipient in recipients:
                    email = recipient['Email']
                    message_id = str(recipient['MessageID'])  # MessageUUID isn't yet useful for other Mailjet APIs
                    recipient_status[email] = AnymailRecipientStatus(message_id=message_id, status=status)
                # Note that for errors, Mailjet doesn't identify the problem recipients.
                # This can occur with a batch send. We patch up the missing recipients below.
        except (KeyError, TypeError):
            raise AnymailRequestsAPIError("Invalid Mailjet API response format",
                                          email_message=message, payload=payload, response=response, backend=self)

        # Any recipient who wasn't reported as a 'success' must have been an error:
        for email in payload.recipients:
            if email.addr_spec not in recipient_status:
                recipient_status[email.addr_spec] = AnymailRecipientStatus(message_id=None, status='failed')

        return recipient_status


class MailjetPayload(RequestsPayload):

    def __init__(self, message, defaults, backend, *args, **kwargs):
        self.esp_extra = {}  # late-bound in serialize_data
        auth = (backend.api_key, backend.secret_key)
        http_headers = {
            'Content-Type': 'application/json',
        }
        self.merge_data = None  # late-bound in _expand_merge_data if present
        self.recipients = []  # for backend parse_recipient_status
        super(MailjetPayload, self).__init__(message, defaults, backend,
                                             auth=auth, headers=http_headers, *args, **kwargs)

    def get_api_endpoint(self):
        return "send"

    def serialize_data(self):
        headers = self.data["Headers"]
        if "Reply-To" in headers:
            # Reply-To must be in its own param
            reply_to = headers.pop('Reply-To')
            self.set_reply_to(parse_address_list([reply_to]))
        if len(headers) > 0:
            self.data["Headers"] = dict(headers)  # flatten to normal dict for json serialization
        else:
            del self.data["Headers"]  # don't send empty headers

        sandbox_mode = self.data.pop("SandboxMode", None)  # hoist to payload root
        payload = {"Messages": [self.data]}
        if sandbox_mode is not None:
            payload["SandboxMode"] = sandbox_mode
        if self.merge_data is not None:
            payload = {"Messages": self._expand_merge_data()}

        return self.serialize_json(payload)

    #
    # Payload construction
    #

    def init_payload(self):
        # the single Messages item, or base to be replicated for merge/batch:
        self.data = {
            "Headers": CaseInsensitiveDict()
        }

    def _expand_merge_data(self):
        """Send bulk mail with different variables for each mail."""
        messages = []
        self.data.setdefault("Variables", {})  # might already have global_merge_data
        for to in self.data.pop("To", []):
            email = to["Email"]
            data = self.data.copy()  # careful: not a deep copy
            data["To"] = [to]
            if email in self.merge_data:
                variables = data["Variables"].copy()  # don't modify the globals
                variables.update(self.merge_data[email])
                data["Variables"] = self._strip_none(variables)
            messages.append(data)
        return messages

    @staticmethod
    def _mailjet_email(email):
        """Expand an Anymail EmailAddress into Mailjet's {"Email", "Name"} dict"""
        # Not documented, but Mailjet seems to allow "", None (json null), or just omitting missing Name
        result = {"Email": email.addr_spec}
        if email.display_name:
            result["Name"] = email.display_name
        return result

    @staticmethod
    def _strip_none(variables):
        """Return dict `variables` omitting any keys with `None` value"""
        # Works around an Mailjet API bug where a null personalization variable results in a message
        # that appears to succeed (with a MessageHref and everything), but never actually gets sent.
        # (Reported to Mailjet ticket #830569 1/2018)
        return {key: value for key, value in variables.items() if value is not None}

    def set_from_email(self, email):
        self.data["From"] = self._mailjet_email(email)

    def set_recipients(self, recipient_type, emails):
        assert recipient_type in ["to", "cc", "bcc"]
        if len(emails) > 0:
            self.data[recipient_type.title()] = [self._mailjet_email(email) for email in emails]
            self.recipients += emails

    def set_subject(self, subject):
        self.data["Subject"] = subject

    def set_reply_to(self, emails):
        if len(emails) > 0:
            self.data["ReplyTo"] = self._mailjet_email(emails[0])
            if len(emails) > 1:
                self.unsupported_feature("Multiple reply_to addresses")

    def set_extra_headers(self, headers):
        self.data["Headers"].update(headers)

    def set_text_body(self, body):
        if body:  # Django's default empty text body confuses Mailjet (esp. templates)
            self.data["TextPart"] = body

    def set_html_body(self, body):
        if body is not None:
            if "HTMLPart" in self.data:
                # second html body could show up through multiple alternatives, or html body + alternative
                self.unsupported_feature("multiple html parts")

            self.data["HTMLPart"] = body

    def add_attachment(self, attachment):
        att = {
            "ContentType": attachment.mimetype,
            "Filename": attachment.name or "",
            "Base64Content": attachment.b64content,
        }
        if attachment.inline:
            field = "InlinedAttachments"
            att["ContentID"] = attachment.cid
        else:
            field = "Attachments"
        self.data.setdefault(field, []).append(att)

    def set_envelope_sender(self, email):
        self.data["Sender"] = email.addr_spec  # ??? v3 docs unclear

    def set_metadata(self, metadata):
        # Mailjet expects a single string payload
        self.data["EventPayload"] = self.serialize_json(metadata)

    def set_tags(self, tags):
        # The choices here are CustomID or Campaign, and Campaign seems closer
        # to how "tags" are handled by other ESPs -- e.g., you can view dashboard
        # statistics across all messages with the same Campaign.
        if len(tags) > 0:
            self.data["CustomCampaign"] = tags[0]
            if len(tags) > 1:
                self.unsupported_feature('multiple tags (%r)' % tags)

    def set_track_clicks(self, track_clicks):
        self.data["TrackClicks"] = "enabled" if track_clicks else "disabled"

    def set_track_opens(self, track_opens):
        self.data["TrackOpens"] = "enabled" if track_opens else "disabled"

    def set_template_id(self, template_id):
        self.data["TemplateID"] = int(template_id)  # Mailjet requires integer (not string)
        self.data["TemplateLanguage"] = True

    def set_merge_data(self, merge_data):
        # Will be handled later in serialize_data
        self.merge_data = merge_data

    def set_merge_global_data(self, merge_global_data):
        self.data["Variables"] = self._strip_none(merge_global_data)

    def set_esp_extra(self, extra):
        # extra gets merged into the payload at the "Messages" item level
        # (and will get replicated for each recipient in a batch send).
        # (But note special handling for SandboxMode in serialize_data.)
        self.data.update(extra)
