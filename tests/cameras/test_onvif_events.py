"""Tests for the ONVIF event SOAP bodies, parsers, and envelope extension.

Fixtures are the verbatim response shapes captured from a live Axis camera during
the event spike, so the parsers are pinned against real wire formats (including
the discovery-vs-live topic-prefix difference and the subscription-reference
quirk where the manager address is the bare services URL plus a SubscriptionId
reference parameter).
"""

from __future__ import annotations

from timelapse_manager.cameras import _onvif_soap as soap
from timelapse_manager.cameras import onvif

# -- Envelope / WS-Addressing -----------------------------------------------


def test_envelope_unchanged_without_extra_headers() -> None:
    # The existing media/device calls must emit a byte-identical envelope: with no
    # credentials and no extra headers there is no <s:Header> at all.
    out = soap.envelope("<trt:GetProfiles/>", None)
    assert "<s:Header>" not in out
    assert "<trt:GetProfiles/>" in out


def test_envelope_adds_extra_headers_and_security() -> None:
    out = soap.envelope(
        "<tev:PullMessages/>", ("user", "pass"), "<wsa:Action>x</wsa:Action>"
    )
    assert "<s:Header>" in out
    assert "<wsa:Action>x</wsa:Action>" in out
    assert "wsse:Security" in out  # credentials still produce the security token


def test_addressing_headers_carry_action_to_and_reference() -> None:
    ref = '<dom0:SubscriptionId xmlns:dom0="urn:x">3</dom0:SubscriptionId>'
    headers = soap.addressing_headers(
        soap.ACTION_PULL_MESSAGES,
        "http://10.0.0.1/onvif/services",
        reference_parameters=ref,
    )
    assert f"<wsa:Action>{soap.ACTION_PULL_MESSAGES}</wsa:Action>" in headers
    assert "<wsa:To>http://10.0.0.1/onvif/services</wsa:To>" in headers
    assert "<wsa:MessageID>urn:uuid:" in headers
    assert "<dom0:SubscriptionId" in headers


# -- Subscription reference parsing ------------------------------------------

_CREATE_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
  xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
  xmlns:wsa5="http://www.w3.org/2005/08/addressing">
  <s:Body>
    <tev:CreatePullPointSubscriptionResponse>
      <tev:SubscriptionReference>
        <wsa5:Address>http://10.1.20.111/onvif/services</wsa5:Address>
        <wsa5:ReferenceParameters>
          <dom0:SubscriptionId xmlns:dom0="http://www.axis.com/2009/event">3</dom0:SubscriptionId>
        </wsa5:ReferenceParameters>
      </tev:SubscriptionReference>
      <wsnt:CurrentTime>2026-06-24T00:20:00Z</wsnt:CurrentTime>
      <wsnt:TerminationTime>2026-06-24T00:25:00Z</wsnt:TerminationTime>
    </tev:CreatePullPointSubscriptionResponse>
  </s:Body>
</s:Envelope>"""


def test_parse_subscription_reference_address_and_refparams() -> None:
    result = soap.parse_subscription_reference(_CREATE_RESPONSE)
    assert result is not None
    address, ref_params = result
    # On this firmware the manager address is the bare services URL.
    assert address == "http://10.1.20.111/onvif/services"
    # The SubscriptionId must round-trip with its namespace declared inline so it
    # stands alone in the outbound header.
    assert "SubscriptionId" in ref_params
    assert "http://www.axis.com/2009/event" in ref_params
    assert ">3<" in ref_params


def test_parse_subscription_reference_none_on_fault() -> None:
    assert soap.parse_subscription_reference("<s:Envelope/>") is None
    assert soap.parse_subscription_reference("not xml") is None


# -- PullMessages parsing ----------------------------------------------------

_PULL_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
  xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
  xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body>
    <tev:PullMessagesResponse>
      <wsnt:NotificationMessage>
        <wsnt:Topic Dialect="...">tns1:Device/tnsaxis:IO/VirtualInput</wsnt:Topic>
        <wsnt:Message>
          <tt:Message UtcTime="2026-06-24T00:23:05Z" PropertyOperation="Changed">
            <tt:Source><tt:SimpleItem Name="port" Value="1"/></tt:Source>
            <tt:Key/>
            <tt:Data><tt:SimpleItem Name="active" Value="1"/></tt:Data>
          </tt:Message>
        </wsnt:Message>
      </wsnt:NotificationMessage>
    </tev:PullMessagesResponse>
  </s:Body>
</s:Envelope>"""


def test_parse_pull_messages_extracts_topic_and_items() -> None:
    notifications = soap.parse_pull_messages(_PULL_RESPONSE)
    assert len(notifications) == 1
    note = notifications[0]
    assert note["topic"] == "tns1:Device/tnsaxis:IO/VirtualInput"
    assert note["utc_time"] == "2026-06-24T00:23:05Z"
    assert note["operation"] == "Changed"
    assert note["source"] == {"port": "1"}
    assert note["data"] == {"active": "1"}


def test_parse_pull_messages_empty_on_no_messages() -> None:
    empty = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
      <s:Body><tev:PullMessagesResponse
        xmlns:tev="http://www.onvif.org/ver10/events/wsdl"/></s:Body></s:Envelope>"""
    assert soap.parse_pull_messages(empty) == []
    assert soap.parse_pull_messages("garbage") == []


# -- GetEventProperties topic-set parsing ------------------------------------

_EVENT_PROPERTIES = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
  xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
  xmlns:wstop="http://docs.oasis-open.org/wsn/t-1"
  xmlns:tns1="http://www.onvif.org/ver10/topics"
  xmlns:tnsaxis="http://www.axis.com/2009/event/topics">
  <s:Body>
    <tev:GetEventPropertiesResponse>
      <wstop:TopicSet>
        <tns1:Device>
          <tns1:IO>
            <tns1:VirtualInput wstop:topic="true"/>
          </tns1:IO>
        </tns1:Device>
        <tns1:VideoSource>
          <tns1:MotionAlarm wstop:topic="true"/>
        </tns1:VideoSource>
      </wstop:TopicSet>
    </tev:GetEventPropertiesResponse>
  </s:Body>
</s:Envelope>"""


def test_parse_event_topics_walks_topic_set() -> None:
    topics = soap.parse_event_topics(_EVENT_PROPERTIES)
    assert "Device/IO/VirtualInput" in topics
    assert "VideoSource/MotionAlarm" in topics


def test_parse_event_topics_empty_on_garbage() -> None:
    assert soap.parse_event_topics("nope") == []


# -- Adapter-level helpers ---------------------------------------------------


def test_event_from_notification_rising_edge() -> None:
    note = soap.parse_pull_messages(_PULL_RESPONSE)[0]
    event = onvif._event_from_notification(note)
    assert event is not None
    # The live topic (with tnsaxis:) canonicalises to the stored discovery key.
    assert event.topic_id == "Device/IO/VirtualInput"
    assert event.category == "io"
    assert event.source == {"port": "1"}
    assert event.data == {"active": "1"}
    assert event.active is True  # rising edge fires


def test_event_from_notification_falling_edge() -> None:
    falling = _PULL_RESPONSE.replace(
        'Name="active" Value="1"', 'Name="active" Value="0"'
    )
    note = soap.parse_pull_messages(falling)[0]
    event = onvif._event_from_notification(note)
    assert event is not None
    assert event.active is False  # clear -> not a rising edge


def test_descriptor_for_canonical_topic() -> None:
    descriptor = onvif._descriptor_for(
        "Device/IO/VirtualInput", "tns1:Device/IO/VirtualInput"
    )
    assert descriptor.topic_id == "Device/IO/VirtualInput"
    assert descriptor.raw_topic == "tns1:Device/IO/VirtualInput"
    assert descriptor.category == "io"
    assert descriptor.stateful is True
    assert descriptor.protocol == "onvif"


def test_parse_events_xaddr_finds_events_service() -> None:
    response = """<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
      xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
      <s:Body><tds:GetServicesResponse>
        <tds:Service>
          <tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>
          <tds:XAddr>http://10.0.0.1/onvif/services</tds:XAddr>
        </tds:Service>
        <tds:Service>
          <tds:Namespace>http://www.onvif.org/ver10/events/wsdl</tds:Namespace>
          <tds:XAddr>http://10.0.0.1/onvif/services</tds:XAddr>
        </tds:Service>
      </tds:GetServicesResponse></s:Body></s:Envelope>"""
    assert onvif._parse_events_xaddr(response) == "http://10.0.0.1/onvif/services"


def test_parse_events_xaddr_none_when_absent() -> None:
    assert onvif._parse_events_xaddr("<s:Envelope/>") is None
