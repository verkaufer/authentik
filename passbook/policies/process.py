"""passbook policy task"""
from multiprocessing import Process
from multiprocessing.connection import Connection
from typing import Optional

from django.core.cache import cache
from structlog import get_logger

from passbook.core.models import User
from passbook.policies.exceptions import PolicyException
from passbook.policies.models import PolicyBinding
from passbook.policies.types import PolicyRequest, PolicyResult

LOGGER = get_logger()


def cache_key(binding: PolicyBinding, request: PolicyRequest) -> str:
    """Generate Cache key for policy"""
    prefix = f"policy_{binding.policy_binding_uuid.hex}_{binding.policy.pk.hex}"
    if request.http_request:
        prefix += f"_{request.http_request.session.session_key}"
    if request.user:
        prefix += f"#{request.user.pk}"
    return prefix


class PolicyProcess(Process):
    """Evaluate a single policy within a seprate process"""

    connection: Connection
    binding: PolicyBinding
    request: PolicyRequest

    def __init__(
        self,
        binding: PolicyBinding,
        request: PolicyRequest,
        connection: Optional[Connection],
    ):
        super().__init__()
        self.binding = binding
        self.request = request
        if connection:
            self.connection = connection

    def execute(self) -> PolicyResult:
        """Run actual policy, returns result"""
        LOGGER.debug(
            "P_ENG(proc): Running policy",
            policy=self.binding.policy,
            user=self.request.user,
            process="PolicyProcess",
        )
        try:
            policy_result = self.binding.policy.passes(self.request)
        except PolicyException as exc:
            LOGGER.debug("P_ENG(proc): error", exc=exc)
            policy_result = PolicyResult(False, str(exc))
        # Invert result if policy.negate is set
        if self.binding.negate:
            policy_result.passing = not policy_result.passing
        LOGGER.debug(
            "P_ENG(proc): Finished",
            policy=self.binding.policy,
            result=policy_result,
            process="PolicyProcess",
            passing=policy_result.passing,
            user=self.request.user,
        )
        key = cache_key(self.binding, self.request)
        cache.set(key, policy_result)
        LOGGER.debug("P_ENG(proc): Cached policy evaluation", key=key)
        return policy_result

    def run(self):
        """Task wrapper to run policy checking"""
        self.connection.send(self.execute())
