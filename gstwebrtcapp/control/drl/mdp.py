from abc import ABCMeta, abstractmethod
import collections
from gymnasium import spaces
import numpy as np
from typing import Any, Dict, OrderedDict, Tuple

from utils.gst import GstWebRTCStatsType, find_stat
from utils.webrtc import clock_units_to_seconds, ntp_short_format_to_seconds


class MDP(metaclass=ABCMeta):
    '''
    MDP is an abstract class for Markov Decision Process. It defines the interface for the environment.
    It also provides methods to translate the environment state and action to the controller's state and action
    that could be directly applied to the GStreamer pipeline and vice versa.
    '''

    MAX_BITRATE_STREAM_MBPS = 15  # so far for 1 stream only, later we can have multiple streams
    MAX_DELAY_SEC = 1  # assume we target the sub-second latency

    CONSTANTS = {
        "MAX_BITRATE_STREAM_MBPS": MAX_BITRATE_STREAM_MBPS,
        "MAX_DELAY_SEC": MAX_DELAY_SEC,
    }

    def __init__(self, episode_length: int) -> None:
        self.episode_length = episode_length
        self.states_made = 0
        self.is_scaled = False
        self.last_stats = None
        self.obs_filter = None

    @abstractmethod
    def reset(self):
        self.states_made = 0
        self.last_stats = None

    @abstractmethod
    def create_observation_space(self) -> spaces.Dict:
        pass

    @abstractmethod
    def create_action_space(self) -> spaces.Space:
        pass

    @abstractmethod
    def make_default_state(self) -> OrderedDict[str, Any]:
        pass

    @abstractmethod
    def make_state(self, stats: Dict[str, Any]) -> OrderedDict[str, Any]:
        self.states_made += 1

    @abstractmethod
    def convert_to_unscaled_state(self, state: OrderedDict[str, Any]) -> OrderedDict[str, Any]:
        pass

    @abstractmethod
    def convert_to_unscaled_action(self, action: Any) -> Any:
        pass

    @abstractmethod
    def check_observation(self, obs: Dict[str, Any]) -> bool:
        pass

    @abstractmethod
    def pack_action_for_controller(self, action: Any) -> Dict[str, Any]:
        pass

    @abstractmethod
    def calculate_reward(self, state: OrderedDict[str, Any]) -> Tuple[float, Dict[str, Any | float] | None]:
        pass

    def is_terminated(self, step: int) -> bool:
        return False

    def is_truncated(self, step) -> bool:
        return step >= self.episode_length

    def get_stat_diff(self, stats: Dict[str, Any], last_stats: Dict[str, Any], stat: str) -> float | int:
        return stats[stat] - last_stats[stat] if last_stats is not None else stats[stat]


class AhoyBrowserMDP(MDP):
    '''
    This MDP is used for the Ahoy engine. This version takes BROWSER stats delivered by GStreamer.
    '''

    def __init__(self, episode_length: int, constants: Dict[str, Any] | None = None) -> None:
        super().__init__(episode_length)
        if constants is not None:
            for key in constants:
                self.CONSTANTS[key] = constants[key]

        # obs are scaled to [0, 1], actions are scaled to [-1, 1]
        self.is_scaled = True
        # filter obs only with needed stats in
        self.obs_filter = [
            GstWebRTCStatsType.RTP_OUTBOUND_STREAM,
            GstWebRTCStatsType.RTP_REMOTE_INBOUND_STREAM,
        ]

        self.reset()

    def reset(self):
        # memento pattern
        super().reset()
        self.rtts = []
        self.discard_first_rtts = 3
        self.last_rate = 0.0

    def create_observation_space(self) -> spaces.Dict:
        # normalized to [0, 1]
        return spaces.Dict(
            {
                "fractionLossRate": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "fractionNackRate": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "fractionPliRate": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "fractionQueueingRtt": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "fractionRtt": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "interarrivalJitter": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "lossRate": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "rttIqr": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "rttMean": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "rttStd": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
                "rxRate": spaces.Box(low=0, high=1, shape=(1,), dtype=np.float32),
            }
        )

    def create_action_space(self) -> spaces.Space:
        # basic AS uses only bitrate, normalized to [-1, 1]
        return spaces.Box(low=-1, high=1, shape=(1,), dtype=np.float32)

    def make_default_state(self) -> OrderedDict[str, Any]:
        return collections.OrderedDict(
            {
                "fractionLossRate": 0.0,
                "fractionNackRate": 0.0,
                "fractionPliRate": 0.0,
                "fractionQueueingRtt": 0.0,
                "fractionRtt": 0.0,
                "interarrivalJitter": 0.0,
                "lossRate": 0.0,
                "rttIqr": 0.0,
                "rttMean": 0.0,
                "rttStd": 0.0,
                "rxRate": 0.0,
            }
        )

    def make_state(self, stats: Dict[str, Any]) -> OrderedDict[str, Any]:
        super().make_state(stats)

        # get dicts with needed stats
        rtp_outbound = find_stat(stats, GstWebRTCStatsType.RTP_OUTBOUND_STREAM)
        rtp_remote_inbound_stream = find_stat(stats, GstWebRTCStatsType.RTP_REMOTE_INBOUND_STREAM)
        if rtp_outbound is None or rtp_remote_inbound_stream is None:
            return self.make_default_state()

        # get previous state for calculating fractional values
        last_rtp_outbound = (
            find_stat(self.last_stats, GstWebRTCStatsType.RTP_OUTBOUND_STREAM) if self.last_stats is not None else None
        )
        last_rtp_remote_inbound_stream = (
            find_stat(self.last_stats, GstWebRTCStatsType.RTP_REMOTE_INBOUND_STREAM)
            if self.last_stats is not None
            else None
        )

        # get needed stats
        packets_sent_diff = self.get_stat_diff(rtp_outbound, last_rtp_outbound, "packets-sent")
        packets_recv_diff = self.get_stat_diff(rtp_outbound, last_rtp_outbound, "packets-received")
        ts_diff_sec = self.get_stat_diff(rtp_outbound, last_rtp_outbound, "timestamp") / 1000

        # loss rates
        # 1. fraction loss rate
        rb_packetslost_diff = self.get_stat_diff(
            rtp_remote_inbound_stream, last_rtp_remote_inbound_stream, "rb-packetslost"
        )
        fraction_loss_rate = rb_packetslost_diff / packets_sent_diff if packets_sent_diff > 0 else 0
        # 7. global loss rate
        loss_rate = (
            rtp_remote_inbound_stream["rb-packetslost"] / rtp_outbound["packets-sent"]
            if rtp_outbound["packets-sent"] > 0
            else 0
        )

        # 2. fraction nack rate
        recv_nack_count_diff = self.get_stat_diff(rtp_outbound, last_rtp_outbound, "nack-count")
        fraction_nack_rate = recv_nack_count_diff / packets_recv_diff if packets_recv_diff > 0 else 0

        # 3. fraction pli rate
        recv_pli_count_diff = self.get_stat_diff(rtp_outbound, last_rtp_outbound, "pli-count")
        fraction_pli_rate = recv_pli_count_diff / packets_recv_diff if packets_recv_diff > 0 else 0

        # rtts: RTT comes in NTP short format
        rtt = ntp_short_format_to_seconds(rtp_remote_inbound_stream["rb-round-trip"]) / self.MAX_DELAY_SEC

        if self.states_made > self.discard_first_rtts:
            # discard first N rtts to avoid initial spikes
            self.rtts.append(rtt)

        # 4. fraction queueing rtt
        fraction_queueing_rtt = rtt - min(self.rtts) if len(self.rtts) > 0 else 0.0
        # 8. iqr rtt
        rtt_iqr = np.quantile(self.rtts, 0.75) - np.quantile(self.rtts, 0.25) if len(self.rtts) > 0 else 0.0
        # 9. mean rtt
        rtt_mean = np.mean(self.rtts) if len(self.rtts) > 0 else 0.0
        # 10. std rtt
        rtt_std = np.std(self.rtts) if len(self.rtts) > 0 else 0.0

        # 6. jitter: comes in clock units
        interarrival_jitter = (
            clock_units_to_seconds(rtp_remote_inbound_stream["rb-jitter"], rtp_outbound["clock-rate"])
            / self.MAX_DELAY_SEC
        )

        # 11. rx rate
        rx_bytes_diff = self.get_stat_diff(rtp_outbound, last_rtp_outbound, "bytes-received")
        rx_mbits_diff = rx_bytes_diff * 8 / 1000000
        rx_rate = rx_mbits_diff / (ts_diff_sec * self.MAX_BITRATE_STREAM_MBPS) if ts_diff_sec > 0 else 0.0

        self.last_stats = stats
        return collections.OrderedDict(
            {
                "fractionLossRate": fraction_loss_rate,
                "fractionNackRate": fraction_nack_rate,
                "fractionPliRate": fraction_pli_rate,
                "fractionQueueingRtt": fraction_queueing_rtt,
                "fractionRtt": rtt,
                "interarrivalJitter": interarrival_jitter,
                "lossRate": loss_rate,
                "rttIqr": rtt_iqr,
                "rttMean": rtt_mean,
                "rttStd": rtt_std,
                "rxRate": rx_rate,
            }
        )

    def convert_to_unscaled_state(self, state: OrderedDict[str, Any]) -> OrderedDict[str, Any]:
        return (
            collections.OrderedDict(
                {
                    "fractionLossRate": state["fractionLossRate"],
                    "fractionNackRate": state["fractionNackRate"],
                    "fractionPliRate": state["fractionPliRate"],
                    "fractionQueueingRtt": state["fractionQueueingRtt"] * self.MAX_DELAY_SEC,
                    "fractionRtt": state["fractionRtt"] * self.MAX_DELAY_SEC,
                    "interarrivalJitter": state["interarrivalJitter"] * self.MAX_DELAY_SEC,
                    "lossRate": state["lossRate"],
                    "rttIqr": state["rttIqr"] * self.MAX_DELAY_SEC,
                    "rttMean": state["rttMean"] * self.MAX_DELAY_SEC,
                    "rttStd": state["rttStd"] * self.MAX_DELAY_SEC,
                    "rxRate": state["rxRate"] * self.MAX_BITRATE_STREAM_MBPS,
                }
            )
            if self.is_scaled
            else state
        )

    def convert_to_unscaled_action(self, action: np.ndarray | float | int) -> np.ndarray | float:
        return 0.5 * (action + 1) * self.MAX_BITRATE_STREAM_MBPS if self.is_scaled else action

    def check_observation(self, obs: Dict[str, Any]) -> bool:
        if self.obs_filter is not None:
            for stat in self.obs_filter:
                if find_stat(obs, stat) is None:
                    return False
        return True

    def pack_action_for_controller(self, action: Any) -> Dict[str, Any]:
        # here we have only bitrate decisions that come as a 1-size np array in mbps (check create_action_space)
        return {"bitrate": self.convert_to_unscaled_action(action)[0] * 1000}

    def calculate_reward(self, state: OrderedDict[str, Any]) -> Tuple[float, Dict[str, Any | float] | None]:
        # TODO:
        return 0.0, None