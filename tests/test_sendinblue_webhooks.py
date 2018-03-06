import json
from datetime import datetime

from django.utils.timezone import utc
from mock import ANY

from anymail.signals import AnymailTrackingEvent
from anymail.webhooks.sendinblue import SendinBlueTrackingWebhookView
from .webhook_cases import WebhookBasicAuthTestsMixin, WebhookTestCase


class SendinBlueWebhookSecurityTestCase(WebhookTestCase, WebhookBasicAuthTestsMixin):
    def call_webhook(self):
        return self.client.post('/anymail/sendinblue/tracking/',
                                content_type='application/json', data=json.dumps({}))

    # Actual tests are in WebhookBasicAuthTestsMixin


class SendinBlueDeliveryTestCase(WebhookTestCase):
    # SendinBlue's webhook payload data doesn't seem to be documented anywhere.
    # There's a list of webhook events at https://apidocs.sendinblue.com/webhooks/#3.
    # The payloads below were obtained through live testing.

    def test_sent_event(self):
        raw_event = {
            "event": "request",
            "email": "recipient@example.com",
            "id": 9999999,  # this appears to be a SendinBlue account id (not an event id)
            "message-id": "<201803062010.27287306012@smtp-relay.mailin.fr>",
            "subject": "Test subject",

            # From a message sent at 2018-03-06T19:10:23Z:
            "date": "2018-03-06 11:10:23",  # what timezone is this?
            "ts": 1520331023,  # 2018-03-06T10:10:23 -- what timezone is this?
            "ts_event": 1520331023,  # unclear if this ever differs from "ts"
            "ts_epoch": 1520363423000,  # 2018-03-06T19:10:23.000Z -- correct!

            "X-Mailin-custom": '{"meta": "data"}',
            # TODO: SendinBlue seems to have a bug where X-Mailin-custom is truncated at the first ':',
            #       only in webhook data (e.g., above would be ` "X-Mailin-custom": '{"meta":' `).
            #       Waiting to hear from their technical team.
            "tag": "test-tag",  # note: for template send, is template name if no other tag provided
            "template_id": 12,
            "sending_ip": "333.33.33.33",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertIsInstance(event, AnymailTrackingEvent)
        self.assertEqual(event.event_type, "sent")
        self.assertEqual(event.esp_event, raw_event)
        self.assertEqual(event.timestamp, datetime(2018, 3, 6, 19, 10, 23, microsecond=0, tzinfo=utc))
        self.assertEqual(event.message_id, "<201803062010.27287306012@smtp-relay.mailin.fr>")
        self.assertIsNone(event.event_id)  # SendinBlue does not provide a unique event id
        self.assertEqual(event.recipient, "recipient@example.com")
        self.assertEqual(event.metadata, {"meta": "data"})
        self.assertEqual(event.tags, ["test-tag"])

    def test_delivered_event(self):
        raw_event = {
            # For brevity, this and following tests omit some webhook data
            # that was tested earlier, or that is not used by Anymail
            "event": "delivered",
            "email": "recipient@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertIsInstance(event, AnymailTrackingEvent)
        self.assertEqual(event.event_type, "delivered")
        self.assertEqual(event.esp_event, raw_event)
        self.assertEqual(event.message_id, "<201803011158.9876543210@smtp-relay.mailin.fr>")
        self.assertEqual(event.recipient, "recipient@example.com")
        self.assertEqual(event.metadata, {})  # empty dict when no X-Mailin-custom header given
        self.assertEqual(event.tags, [])  # empty list when no tags given

    def test_hard_bounce(self):
        # "Emails which were not delivered and which will never arrive at destination.
        # Emails are misspelled or do not exist."
        # TODO: capture actual hard_bounce event (payload below is a guess)
        raw_event = {
            "event": "hard_bounce",
            "email": "recipient@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
            "reason": "(guessing hard_bounce includes a reason)",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "bounced")
        self.assertEqual(event.reject_reason, "bounced")
        self.assertEqual(event.mta_response, "(guessing hard_bounce includes a reason)")

    def test_soft_bounce_event(self):
        raw_event = {
            "event": "soft_bounce",
            "email": "recipient@no-mx.example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
            "reason": "undefined Unable to find MX of domain no-mx.example.com",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "bounced")
        self.assertEqual(event.reject_reason, "bounced")
        self.assertIsNone(event.description)  # no human-readable description consistently available
        self.assertEqual(event.mta_response, "undefined Unable to find MX of domain no-mx.example.com")

    def test_blocked(self):
        # "These are spam report emails + hard bounce emails when they are repeated."
        # TODO: capture actual blocked event (payload below is a guess)
        raw_event = {
            "event": "blocked",
            "email": "recipient@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
            "reason": "(guessing blocked includes a reason)",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "rejected")
        self.assertEqual(event.reject_reason, "blocked")
        self.assertEqual(event.mta_response, "(guessing blocked includes a reason)")

    def test_spam(self):
        # "When a person who received your email reported that it is a spam."
        # TODO: capture actual spam event (payload below is a guess)
        raw_event = {
            "event": "spam",
            "email": "recipient@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "complained")

    def test_invalid_email(self):
        # "If a ISP again indicated us that the email is not valid or if we discovered that the email is not valid."
        # TODO: capture actual invalid_email event (payload below is a guess)
        raw_event = {
            "event": "invalid_email",
            "email": "recipient@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
            "reason": "(guessing invalid_email includes a reason)",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "bounced")
        self.assertEqual(event.reject_reason, "invalid")
        self.assertEqual(event.mta_response, "(guessing invalid_email includes a reason)")

    def test_deferred_event(self):
        # Note: the example below is an actual event capture (with 'example.com' substituted
        # for the real receiving domain). It's pretty clearly a bounce, not a deferral.
        # It looks like SendinBlue mis-categorizes this SMTP response code.
        raw_event = {
            "event": "deferred",
            "email": "notauser@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
            "reason": "550 RecipientError: 550 5.1.1 <notauser@example.com>: Recipient address rejected: "
                      "User unknown in virtual alias table",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "deferred")
        self.assertIsNone(event.description)  # no human-readable description consistently available
        self.assertEqual(event.mta_response,
                         "550 RecipientError: 550 5.1.1 <notauser@example.com>: Recipient address rejected: "
                         "User unknown in virtual alias table")

    def test_opened_event(self):
        raw_event = {
            "event": "opened",
            "email": "recipient@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "opened")
        self.assertIsNone(event.user_agent)  # SendinBlue doesn't report user agent

    def test_unique_opened_event(self):
        # SendinBlue delivers unique_opened *and* opened on the first open.
        # To avoid double-counting, Anymail normalizes unique_opened to UNKNOWN rather than OPENED.
        raw_event = {
            "event": "unique_opened",
            "email": "recipient@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "unknown")

    def test_clicked_event(self):
        raw_event = {
            "event": "click",
            "email": "recipient@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
            "link": "https://example.com/click/me",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "clicked")
        self.assertEqual(event.click_url, "https://example.com/click/me")
        self.assertIsNone(event.user_agent)  # SendinBlue doesn't report user agent

    def test_unsubscribe(self):
        # "When a person unsubscribes from the email received."
        # TODO: capture actual unsubscribe event (payload below is a guess)
        raw_event = {
            "event": "unsubscribe",
            "email": "recipient@example.com",
            "ts": 1519901895,
            "message-id": "<201803011158.9876543210@smtp-relay.mailin.fr>",
        }
        response = self.client.post('/anymail/sendinblue/tracking/',
                                    content_type='application/json', data=json.dumps(raw_event))
        self.assertEqual(response.status_code, 200)
        kwargs = self.assert_handler_called_once_with(self.tracking_handler, sender=SendinBlueTrackingWebhookView,
                                                      event=ANY, esp_name='SendinBlue')
        event = kwargs['event']
        self.assertEqual(event.event_type, "unsubscribed")
