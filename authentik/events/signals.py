"""authentik events signal listener"""

from typing import Any

from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.db.models.signals import post_save, pre_delete
from django.dispatch import receiver
from django.http import HttpRequest

from authentik.core.middleware import SESSION_KEY_IMPERSONATE_USER
from authentik.core.models import User, UserPasswordHistory
from authentik.core.signals import login_failed, password_changed
from authentik.events.apps import SYSTEM_TASK_STATUS
from authentik.events.models import Event, EventAction, SystemTask
from authentik.events.tasks import event_notification_handler, gdpr_cleanup
from authentik.flows.models import Stage
from authentik.flows.planner import PLAN_CONTEXT_SOURCE, FlowPlan
from authentik.flows.views.executor import SESSION_KEY_PLAN
from authentik.policies.models import PolicyBinding
from authentik.policies.password.models import UniquePasswordPolicy
from authentik.root.monitoring import monitoring_set
from authentik.stages.invitation.models import Invitation
from authentik.stages.invitation.signals import invitation_used
from authentik.stages.password.stage import PLAN_CONTEXT_METHOD, PLAN_CONTEXT_METHOD_ARGS
from authentik.stages.user_write.signals import user_write
from authentik.tenants.utils import get_current_tenant

SESSION_LOGIN_EVENT = "login_event"


@receiver(user_logged_in)
def on_user_logged_in(sender, request: HttpRequest, user: User, **_):
    """Log successful login"""
    kwargs = {}
    if SESSION_KEY_PLAN in request.session:
        flow_plan: FlowPlan = request.session[SESSION_KEY_PLAN]
        if PLAN_CONTEXT_SOURCE in flow_plan.context:
            # Login request came from an external source, save it in the context
            kwargs[PLAN_CONTEXT_SOURCE] = flow_plan.context[PLAN_CONTEXT_SOURCE]
        if PLAN_CONTEXT_METHOD in flow_plan.context:
            # Save the login method used
            kwargs[PLAN_CONTEXT_METHOD] = flow_plan.context[PLAN_CONTEXT_METHOD]
            kwargs[PLAN_CONTEXT_METHOD_ARGS] = flow_plan.context.get(PLAN_CONTEXT_METHOD_ARGS, {})
    event = Event.new(EventAction.LOGIN, **kwargs).from_http(request, user=user)
    request.session[SESSION_LOGIN_EVENT] = event


def get_login_event(request: HttpRequest) -> Event | None:
    """Wrapper to get login event that can be mocked in tests"""
    return request.session.get(SESSION_LOGIN_EVENT, None)


@receiver(user_logged_out)
def on_user_logged_out(sender, request: HttpRequest, user: User, **kwargs):
    """Log successfully logout"""
    # Check if this even comes from the user_login stage's middleware, which will set an extra
    # argument
    event = Event.new(EventAction.LOGOUT)
    if "event_extra" in kwargs:
        event.context.update(kwargs["event_extra"])
    event.from_http(request, user=user)


@receiver(user_write)
def on_user_write(sender, request: HttpRequest, user: User, data: dict[str, Any], **kwargs):
    """Log User write"""
    data["created"] = kwargs.get("created", False)
    Event.new(EventAction.USER_WRITE, **data).from_http(request, user=user)

    user_changed_own_password = (
        any("password" in x for x in data.keys())
        and request.user.pk == user.pk
        and SESSION_KEY_IMPERSONATE_USER not in request.session
    )
    if user_changed_own_password:
        # Only save the password if a bound policy requires it
        unique_password_policies = UniquePasswordPolicy.objects.all()

        unique_pwd_policy_binding = PolicyBinding.objects.filter(
            policy__in=unique_password_policies
        ).filter(enabled=True)

        if unique_pwd_policy_binding.exists():
            """NOTE: Because we run this in a signal after saving the user, 
            we are not atomically guaranteed to save password history.
            """
            UserPasswordHistory.objects.create(user=user, change={"old_password": user.password})


@receiver(login_failed)
def on_login_failed(
    signal,
    sender,
    credentials: dict[str, str],
    request: HttpRequest,
    stage: Stage | None = None,
    **kwargs,
):
    """Failed Login, authentik custom event"""
    user = User.objects.filter(username=credentials.get("username")).first()
    Event.new(EventAction.LOGIN_FAILED, **credentials, stage=stage, **kwargs).from_http(
        request, user
    )


@receiver(invitation_used)
def on_invitation_used(sender, request: HttpRequest, invitation: Invitation, **_):
    """Log Invitation usage"""
    Event.new(EventAction.INVITE_USED, invitation_uuid=invitation.invite_uuid.hex).from_http(
        request
    )


@receiver(password_changed)
def on_password_changed(sender, user: User, password: str, **_):
    """Log password change"""
    Event.new(EventAction.PASSWORD_SET).from_http(None, user=user)


@receiver(post_save, sender=Event)
def event_post_save_notification(sender, instance: Event, **_):
    """Start task to check if any policies trigger an notification on this event"""
    event_notification_handler.delay(instance.event_uuid.hex)


@receiver(pre_delete, sender=User)
def event_user_pre_delete_cleanup(sender, instance: User, **_):
    """If gdpr_compliance is enabled, remove all the user's events"""
    if get_current_tenant().gdpr_compliance:
        gdpr_cleanup.delay(instance.pk)


@receiver(monitoring_set)
def monitoring_system_task(sender, **_):
    """Update metrics when task is saved"""
    SYSTEM_TASK_STATUS.clear()
    for task in SystemTask.objects.all():
        task.update_metrics()
