import json

import requests
from django.http import HttpResponse
from django.utils.dateparse import parse_datetime

from .base import AnymailBaseWebhookView
from ..exceptions import AnymailWebhookValidationFailure
from ..signals import tracking, AnymailTrackingEvent, EventType, RejectReason
from ..utils import get_anymail_setting


class AmazonSESBaseWebhookView(AnymailBaseWebhookView):
    """Base view class for Amazon SES webhooks (SNS Notifications)"""

    esp_name = "Amazon SES"

    def __init__(self, **kwargs):
        # whether to automatically respond to SNS SubscriptionConfirmation requests; default True
        # (Future: could also take a TopicArn or list to auto-confirm)
        self.auto_confirm_enabled = get_anymail_setting(
            "auto_confirm_sns_subscriptions", esp_name=self.esp_name, kwargs=kwargs, default=True)
        super(AmazonSESBaseWebhookView, self).__init__(**kwargs)

    @staticmethod
    def _parse_sns_message(request):
        # cache so we don't have to parse the json multiple times
        if not hasattr(request, '_sns_message'):
            try:
                body = request.body.decode(request.encoding or 'utf-8')
                request._sns_message = json.loads(body)
            except (TypeError, ValueError, UnicodeDecodeError) as err:
                raise AnymailWebhookValidationFailure("Malformed SNS message body %r" % request.body,
                                                      raised_from=err)
        return request._sns_message

    def validate_request(self, request):
        # Block random posts that don't even have matching SNS headers
        sns_message = self._parse_sns_message(request)
        header_type = request.META.get("HTTP_X_AMZ_SNS_MESSAGE_TYPE", "<<missing>>")
        body_type = sns_message.get("Type", "<<missing>>")
        if header_type != body_type:
            raise AnymailWebhookValidationFailure(
                'SNS header "x-amz-sns-message-type: %s" doesn\'t match body "Type": "%s"'
                % (header_type, body_type))

        if header_type not in ["Notification", "SubscriptionConfirmation", "UnsubscribeConfirmation"]:
            raise AnymailWebhookValidationFailure("Unknown SNS message type '%s'" % header_type)

        header_id = request.META.get("HTTP_X_AMZ_SNS_MESSAGE_ID", "<<missing>>")
        body_id = sns_message.get("MessageId", "<<missing>>")
        if header_id != body_id:
            raise AnymailWebhookValidationFailure(
                'SNS header "x-amz-sns-message-id: %s" doesn\'t match body "MessageId": "%s"'
                % (header_id, body_id))

        # TODO: Verify SNS message signature
        # https://docs.aws.amazon.com/sns/latest/dg/SendMessageToHttp.verify.signature.html
        # Requires ability to public-key-decrypt signature with Amazon-supplied X.509 cert
        # (which isn't in Python standard lib; need pyopenssl or pycryptodome, e.g.)

    def post(self, request, *args, **kwargs):
        # request has *not* yet been validated at this point
        if self.basic_auth and not request.META.get("HTTP_AUTHORIZATION"):
            # Amazon SNS requires a proper 401 response before it will attempt to send basic auth
            response = HttpResponse(status=401)
            response["WWW-Authenticate"] = 'Basic realm="Anymail WEBHOOK_SECRET"'
            return response
        return super(AmazonSESBaseWebhookView, self).post(request, *args, **kwargs)

    def parse_events(self, request):
        # request *has* been validated by now
        events = []
        sns_message = self._parse_sns_message(request)
        sns_type = sns_message.get("Type")
        if sns_type == "Notification":
            message_string = sns_message.get("Message")
            try:
                ses_event = json.loads(message_string)
            except (TypeError, ValueError):
                if message_string == "Successfully validated SNS topic for Amazon SES event publishing.":
                    pass  # this Notification is generated after SubscriptionConfirmation
                else:
                    raise AnymailWebhookValidationFailure("Unparsable SNS Message %r" % message_string)
            else:
                events = self.esp_to_anymail_events(ses_event, sns_message)
        elif sns_type == "SubscriptionConfirmation":
            self.auto_confirm_sns_subscription(sns_message)
        # else: just ignore other SNS messages (e.g., "UnsubscribeConfirmation")
        return events

    def esp_to_anymail_events(self, ses_event, sns_message):
        raise NotImplementedError()

    def auto_confirm_sns_subscription(self, sns_message):
        """Automatically accept a subscription to Amazon SNS topics, if the request is expected.

        If an SNS SubscriptionConfirmation arrives with HTTP basic auth proving it is meant for us,
        automatically load the SubscribeURL to confirm the subscription.
        """
        if not self.auto_confirm_enabled:
            return

        if not self.basic_auth:
            # Note: basic_auth (shared secret) confirms the notification was meant for us.
            # If WEBHOOK_SECRET isn't set, Anymail logs a warning but allows the request.
            # (Also, verifying the SNS message signature would be insufficient here:
            # if someone else tried to point their own SNS topic at our webhook url,
            # SNS would send a SubscriptionConfirmation with a valid Amazon signature.)
            raise AnymailWebhookValidationFailure(
                "Anymail received an unexpected SubscriptionConfirmation request for Amazon SNS topic "
                "'{topic_arn!s}'. (Anymail can automatically confirm SNS subscriptions if you set a "
                "WEBHOOK_SECRET and use that in your SNS notification url. Or you can manually confirm "
                "this subscription in the SNS dashboard with token '{token!s}'.)"
                "".format(topic_arn=sns_message.get('TopicArn'), token=sns_message.get('Token')))

        # WEBHOOK_SECRET *is* set, so the request's basic auth has been verified by now (in run_validators)
        response = requests.get(sns_message["SubscribeURL"])
        if not response.ok:
            raise AnymailWebhookValidationFailure(
                "Anymail received a {status_code} error trying to automatically confirm a subscription "
                "to Amazon SNS topic '{topic_arn!s}'. The response was '{text!s}'."
                "".format(status_code=response.status_code, text=response.text,
                          topic_arn=sns_message.get('TopicArn')))


class AmazonSESTrackingWebhookView(AmazonSESBaseWebhookView):
    """Handler for Amazon SES tracking notifications"""

    signal = tracking

    def esp_to_anymail_events(self, ses_event, sns_message):
        return []  # TODO
