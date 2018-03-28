import json
import warnings
from datetime import datetime

from django.test import override_settings
from django.utils.timezone import utc
from mock import ANY, patch

from anymail.exceptions import AnymailInsecureWebhookWarning
from anymail.signals import AnymailTrackingEvent
from anymail.webhooks.amazon_ses import AmazonSESTrackingWebhookView

from .mock_requests_backend import RequestsBackendMockAPITestCase
from .webhook_cases import WebhookBasicAuthTestsMixin, WebhookTestCase


class AmazonSESWebhookTestsMixin(object):
    def post_from_sns(self, path, raw_sns_message, **kwargs):
        # noinspection PyUnresolvedReferences
        return self.client.post(
            path,
            content_type='text/plain; charset=UTF-8',  # SNS posts JSON as text/plain
            data=json.dumps(raw_sns_message),
            HTTP_X_AMZ_SNS_MESSAGE_ID=raw_sns_message["MessageId"],
            HTTP_X_AMZ_SNS_MESSAGE_TYPE=raw_sns_message["Type"],
            # Anymail doesn't use other x-amz-sns-* headers
            **kwargs)


class AmazonSESWebhookSecurityTests(WebhookTestCase, AmazonSESWebhookTestsMixin, WebhookBasicAuthTestsMixin):
    def call_webhook(self):
        return self.post_from_sns('/anymail/amazon_ses/tracking/',
                                  {"Type": "Notification", "MessageId": "123", "Message": "{}"})

    # Most actual tests are in WebhookBasicAuthTestsMixin

    def test_verifies_missing_auth(self):
        # Must handle missing auth header slightly differently from Anymail default 400 SuspiciousOperation:
        # SNS will only send basic auth after missing auth responds 401 WWW-Authenticate: Basic realm="..."
        self.clear_basic_auth()
        response = self.call_webhook()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], 'Basic realm="Anymail WEBHOOK_SECRET"')


class AmazonSESNotificationsTests(WebhookTestCase, AmazonSESWebhookTestsMixin):
    def test_bounce_event(self):
        # https://docs.aws.amazon.com/ses/latest/DeveloperGuide/notification-examples.html#notification-examples-bounce
        raw_ses_event = {
            "notificationType": "Bounce",
            "bounce": {
                "bounceType": "Permanent",
                "reportingMTA": "dns; email.example.com",
                "bouncedRecipients": [{
                    "emailAddress": "jane@example.com",
                    "status": "5.1.1",
                    "action": "failed",
                    "diagnosticCode": "smtp; 550 5.1.1 <jane@example.com>... User unknown"
                }],
                "bounceSubType": "General",
                "timestamp": "2016-01-27T14:59:44.101Z",  # when bounce sent (by receiving ISP)
                "feedbackId": "00000138111222aa-44455566-cccc-cccc-cccc-ddddaaaa068a-000000",  # unique id for bounce
                "remoteMtaIp": "127.0.2.0"
            },
            "mail": {
                "timestamp": "2016-01-27T14:59:38.237Z",  # when message sent
                "source": "john@example.com",
                "sourceArn": "arn:aws:ses:us-west-2:888888888888:identity/example.com",
                "sourceIp": "127.0.3.0",
                "sendingAccountId": "123456789012",
                "messageId": "00000138111222aa-33322211-cccc-cccc-cccc-ddddaaaa0680-000000",
                "destination": ["jane@example.com", "mary@example.com", "richard@example.com"],
                "headersTruncated": False,
                "headers": [
                    {"name": "From", "value": '"John Doe" <john@example.com>'},
                    {"name": "To", "value": '"Jane Doe" <jane@example.com>, "Mary Doe" <mary@example.com>,'
                                            ' "Richard Doe" <richard@example.com>'},
                    {"name": "Message-ID", "value": "custom-message-ID"},
                    {"name": "Subject", "value": "Hello"},
                    {"name": "Content-Type", "value": 'text/plain; charset="UTF-8"'},
                    {"name": "Content-Transfer-Encoding", "value": "base64"},
                    {"name": "Date", "value": "Wed, 27 Jan 2016 14:05:45 +0000"}
                ],
                "commonHeaders": {
                    "from": ["John Doe <john@example.com>"],
                    "date": "Wed, 27 Jan 2016 14:05:45 +0000",
                    "to": ["Jane Doe <jane@example.com>, Mary Doe <mary@example.com>,"
                           " Richard Doe <richard@example.com>"],
                    "messageId": "custom-message-ID",
                    "subject": "Hello"
                }
            }
        }
        raw_sns_event = {
            "Type": "Notification",
            "MessageId": "19ba9823-d7f2-53c1-860e-cb10e0d13dfc",  # unique id for SNS event
            "TopicArn": "arn:aws:sns:us-east-1:1234567890:SES_Events",
            "Subject": "Amazon SES Email Event Notification",
            "Message": json.dumps(raw_ses_event) + "\n",
            "Timestamp": "2018-03-26T17:58:59.675Z",
            "SignatureVersion": "1",
            "Signature": "EXAMPLE-SIGNATURE==",
            "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-12345abcde.pem",
            "UnsubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=Unsubscribe&SubscriptionArn=arn...",
        }

        response = self.post_from_sns('/anymail/amazon_ses/tracking/', raw_sns_event)
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=AmazonSESTrackingWebhookView,
                                                      event=ANY, esp_name='Amazon SES')
        event = kwargs['event']
        self.assertIsInstance(event, AnymailTrackingEvent)
        self.assertEqual(event.event_type, "bounced")
        self.assertEqual(event.esp_event, raw_ses_event)
        self.assertEqual(event.timestamp, datetime(2016, 1, 27, 14, 59, 44, microsecond=101000, tzinfo=utc))
        self.assertEqual(event.message_id, "00000138111222aa-33322211-cccc-cccc-cccc-ddddaaaa0680-000000")
        self.assertEqual(event.event_id, "19ba9823-d7f2-53c1-860e-cb10e0d13dfc")
        self.assertEqual(event.recipient, "jane@example.com")
        self.assertEqual(event.reject_reason, "bounced")
        self.assertEqual(event.description,
                         "The server was unable to deliver your message (ex: unknown user, mailbox not found).")
        self.assertEqual(event.mta_response, "smtp; 550 5.1.1 <jane@example.com>... User unknown")


class AmazonSESSubscriptionManagementTests(WebhookTestCase, AmazonSESWebhookTestsMixin):
    # Anymail will automatically respond to SNS subscription notifications
    # if Anymail is configured to require basic auth via WEBHOOK_SECRET.
    # (Note that WebhookTestCase sets up ANYMAIL WEBHOOK_SECRET.)

    # Borrow requests mocking from RequestsBackendMockAPITestCase
    MockResponse = RequestsBackendMockAPITestCase.MockResponse

    def setUp(self):
        super(AmazonSESSubscriptionManagementTests, self).setUp()
        self.patch_request = patch('anymail.webhooks.amazon_ses.requests.get', autospec=True)
        self.mock_request = self.patch_request.start()
        self.addCleanup(self.patch_request.stop)
        self.mock_request.return_value = self.MockResponse(status_code=200)

    SNS_SUBSCRIPTION_CONFIRMATION = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "165545c9-2a5c-472c-8df2-7ff2be2b3b1b",
        "Token": "EXAMPLE_TOKEN",
        "TopicArn": "arn:aws:sns:us-west-2:123456789012:SES_Notifications",
        "Message": "You have chosen to subscribe ...\nTo confirm..., visit the SubscribeURL included in this message.",
        "SubscribeURL": "https://sns.us-west-2.amazonaws.com/?Action=ConfirmSubscription&TopicArn=...",
        "Timestamp": "2012-04-26T20:45:04.751Z",
        "SignatureVersion": "1",
        "Signature": "EXAMPLE-SIGNATURE==",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-12345abcde.pem"
    }

    def test_sns_subscription_auto_confirmation(self):
        """Anymail webhook will auto-confirm SNS topic subscriptions"""
        response = self.post_from_sns('/anymail/amazon_ses/tracking/', self.SNS_SUBSCRIPTION_CONFIRMATION)
        self.assertEqual(response.status_code, 200)
        # auto-visited the SubscribeURL:
        self.mock_request.assert_called_once_with(
            "https://sns.us-west-2.amazonaws.com/?Action=ConfirmSubscription&TopicArn=...")
        # didn't notify receivers:
        self.assertEqual(self.tracking_handler.call_count, 0)
        self.assertEqual(self.inbound_handler.call_count, 0)

    def test_sns_subscription_confirmation_failure(self):
        """Auto-confirmation notifies if SubscribeURL errors"""
        self.mock_request.return_value = self.MockResponse(status_code=500, raw=b"Gateway timeout")
        with self.assertLogs('django.security.AnymailWebhookValidationFailure') as cm:
            response = self.post_from_sns('/anymail/amazon_ses/tracking/', self.SNS_SUBSCRIPTION_CONFIRMATION)
        self.assertEqual(response.status_code, 400)  # bad request
        self.assertEqual(
            ["Anymail received a 500 error trying to automatically confirm a subscription to Amazon SNS topic "
             "'arn:aws:sns:us-west-2:123456789012:SES_Notifications'. The response was 'Gateway timeout'."],
            [record.getMessage() for record in cm.records])
        # auto-visited the SubscribeURL:
        self.mock_request.assert_called_once_with(
            "https://sns.us-west-2.amazonaws.com/?Action=ConfirmSubscription&TopicArn=...")
        # didn't notify receivers:
        self.assertEqual(self.tracking_handler.call_count, 0)
        self.assertEqual(self.inbound_handler.call_count, 0)

    @override_settings(ANYMAIL={})  # clear WEBHOOK_SECRET setting from base WebhookTestCase
    def test_sns_subscription_confirmation_auth_disabled(self):
        """Anymail *won't* auto-confirm SNS subscriptions if WEBHOOK_SECRET isn't in use"""
        warnings.simplefilter("ignore", AnymailInsecureWebhookWarning)  # (this gets tested elsewhere)
        with self.assertLogs('django.security.AnymailWebhookValidationFailure') as cm:
            response = self.post_from_sns('/anymail/amazon_ses/tracking/', self.SNS_SUBSCRIPTION_CONFIRMATION)
        self.assertEqual(response.status_code, 400)  # bad request
        self.assertEqual(
            ["Anymail received an unexpected SubscriptionConfirmation request for Amazon SNS topic "
             "'arn:aws:sns:us-west-2:123456789012:SES_Notifications'. (Anymail can automatically confirm "
             "SNS subscriptions if you set a WEBHOOK_SECRET and use that in your SNS notification url. Or "
             "you can manually confirm this subscription in the SNS dashboard with token 'EXAMPLE_TOKEN'.)"],
            [record.getMessage() for record in cm.records])
        # *didn't* visit the SubscribeURL:
        self.assertEqual(self.mock_request.call_count, 0)
        # didn't notify receivers:
        self.assertEqual(self.tracking_handler.call_count, 0)
        self.assertEqual(self.inbound_handler.call_count, 0)

    def test_sns_confirmation_success_notification(self):
        """Anymail ignores the 'Successfully validated' notification after confirming an SNS subscription"""
        response = self.post_from_sns('/anymail/amazon_ses/tracking/', {
            "Type": "Notification",
            "MessageId": "7fbca0d9-eeab-5285-ae27-f3f57f2e84b0",
            "TopicArn": "arn:aws:sns:us-west-2:123456789012:SES_Notifications",
            "Message": "Successfully validated SNS topic for Amazon SES event publishing.",
            "Timestamp": "2018-03-21T16:58:45.077Z",
            "SignatureVersion": "1",
            "Signature": "EXAMPLE_SIGNATURE==",
            "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-12345abcde.pem",
            "UnsubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=Unsubscribe...",
        })
        self.assertEqual(response.status_code, 200)
        # didn't notify receivers:
        self.assertEqual(self.tracking_handler.call_count, 0)
        self.assertEqual(self.inbound_handler.call_count, 0)

    def test_sns_unsubscribe_confirmation(self):
        """Anymail ignores the UnsubscribeConfirmation SNS message after deleting a subscription"""
        response = self.post_from_sns('/anymail/amazon_ses/tracking/', {
            "Type": "UnsubscribeConfirmation",
            "MessageId": "47138184-6831-46b8-8f7c-afc488602d7d",
            "Token": "EXAMPLE_TOKEN",
            "TopicArn": "arn:aws:sns:us-west-2:123456789012:SES_Notifications",
            "Message": "You have chosen to deactivate subscription ...\nTo cancel ... visit the SubscribeURL...",
            "SubscribeURL": "https://sns.us-west-2.amazonaws.com/?Action=ConfirmSubscription&TopicArn=...",
            "Timestamp": "2012-04-26T20:06:41.581Z",
            "SignatureVersion": "1",
            "Signature": "EXAMPLE_SIGNATURE==",
            "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-12345abcde.pem",
        })
        self.assertEqual(response.status_code, 200)
        # *didn't* visit the SubscribeURL (because that would re-enable the subscription!):
        self.assertEqual(self.mock_request.call_count, 0)
        # didn't notify receivers:
        self.assertEqual(self.tracking_handler.call_count, 0)
        self.assertEqual(self.inbound_handler.call_count, 0)

    @override_settings(ANYMAIL_AMAZON_SES_AUTO_CONFIRM_SNS_SUBSCRIPTIONS=False)
    def test_disable_auto_confirmation(self):
        """The ANYMAIL setting AMAZON_SES_AUTO_CONFIRM_SNS_SUBSCRIPTIONS will disable this feature"""
        response = self.post_from_sns('/anymail/amazon_ses/tracking/', self.SNS_SUBSCRIPTION_CONFIRMATION)
        self.assertEqual(response.status_code, 200)
        # *didn't* visit the SubscribeURL:
        self.assertEqual(self.mock_request.call_count, 0)
        # didn't notify receivers:
        self.assertEqual(self.tracking_handler.call_count, 0)
        self.assertEqual(self.inbound_handler.call_count, 0)
