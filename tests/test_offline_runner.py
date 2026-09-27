import socket

from tools.run_offline_tests import is_internet_socket_event


class _PoisonArgument:
    @property
    def family(self):
        raise AssertionError("non-network audit event must not inspect arguments")


def test_non_socket_audit_events_do_not_inspect_arbitrary_arguments():
    assert not is_internet_socket_event("ctypes.dlsym", (_PoisonArgument(),))


def test_socket_audit_event_checks_internet_socket_family():
    class _SocketLike:
        family = socket.AF_INET

    assert not is_internet_socket_event("socket.connect", (_SocketLike(), ("127.0.0.1", 8000)))
    assert is_internet_socket_event("socket.connect", (_SocketLike(), ("198.51.100.1", 443)))
