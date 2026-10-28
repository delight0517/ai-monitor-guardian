"""AI debug-display manager contracts and safe session coordinator."""

from .models import API_VERSION
from .policy import PolicyDenied
from .service import DisplaySessionService, NoDisplayAdapter
from .private_desktop import PrivateDesktopSession
from .reconnect_recovery import ReconnectRecovery
from .placement_journal import PlacementJournal
from .guest_session import GuestSessionController, GuestSessionError, GuestSessionStatus
from .broker_protocol import BrokerProtocolError, BrokerRequest, BrokerReply, parse_request, encode_reply
from .evolution import EvolutionError, EvolutionModuleHost, ModuleCandidate, ModuleStatus
from .feature_claims import FeatureClaimCoordinator, FeatureClaimError, FeatureClaimInfo

__version__ = "0.7.0"

__all__ = ["API_VERSION", "DisplaySessionService", "NoDisplayAdapter", "PrivateDesktopSession", "ReconnectRecovery", "PlacementJournal", "GuestSessionController", "GuestSessionError", "GuestSessionStatus", "BrokerProtocolError", "BrokerRequest", "BrokerReply", "parse_request", "encode_reply", "EvolutionError", "EvolutionModuleHost", "ModuleCandidate", "ModuleStatus", "FeatureClaimCoordinator", "FeatureClaimError", "FeatureClaimInfo", "PolicyDenied", "__version__"]
