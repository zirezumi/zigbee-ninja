import asyncio

from zigbee_ninja.ingest.mqtt import DISCOVERY_TOPICS, MqttIngest


class RecordingClient:
    def __init__(self):
        self.subscriptions: list = []

    async def subscribe(self, topic):
        self.subscriptions.append(topic)


def test_discovery_topics_subscribed_before_firehose():
    client = RecordingClient()
    asyncio.run(MqttIngest._subscribe(client))

    # The bridge discovery topics must precede the broad "#"/$SYS subscribe so
    # their retained messages arrive before a large retained flood (regression:
    # a "#"-only subscribe lost z2m-*/bridge/info on a busy HA broker).
    assert client.subscriptions[: len(DISCOVERY_TOPICS)] == list(DISCOVERY_TOPICS)
    assert client.subscriptions[-1] == [("#", 0), ("$SYS/#", 0)]

    firehose_index = client.subscriptions.index([("#", 0), ("$SYS/#", 0)])
    for topic in DISCOVERY_TOPICS:
        assert client.subscriptions.index(topic) < firehose_index
