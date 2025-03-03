import copy
import json
import logging
import mock
import pytest
import socket

from uaclient.contract import (
    API_V1_CONTEXT_MACHINE_TOKEN,
    API_V1_RESOURCES,
    API_V1_TMPL_CONTEXT_MACHINE_TOKEN_RESOURCE,
    API_V1_TMPL_RESOURCE_MACHINE_ACCESS,
    ContractAPIError,
    UAContractClient,
    get_available_resources,
    process_entitlement_delta,
    request_updated_contract,
)
from uaclient import exceptions
from uaclient import util
from uaclient.status import (
    MESSAGE_ATTACH_EXPIRED_TOKEN,
    MESSAGE_ATTACH_FAILURE_DEFAULT_SERVICES,
    MESSAGE_ATTACH_FORBIDDEN,
    MESSAGE_ATTACH_FORBIDDEN_EXPIRED,
    MESSAGE_ATTACH_FORBIDDEN_NOT_YET,
    MESSAGE_ATTACH_FORBIDDEN_NEVER,
    MESSAGE_ATTACH_INVALID_TOKEN,
    MESSAGE_UNEXPECTED_ERROR,
)
from uaclient.version import get_version

from uaclient.testing.fakes import FakeContractClient


M_PATH = "uaclient.contract."
M_REPO_PATH = "uaclient.entitlements.repo.RepoEntitlement."


@mock.patch("uaclient.serviceclient.UAServiceClient.request_url")
@mock.patch("uaclient.contract.util.get_machine_id")
class TestUAContractClient:
    @pytest.mark.parametrize(
        "detach,expected_http_method",
        ((None, "POST"), (False, "POST"), (True, "DELETE")),
    )
    @mock.patch("uaclient.contract.util.get_platform_info")
    def test__request_machine_token_update(
        self,
        get_platform_info,
        get_machine_id,
        request_url,
        detach,
        expected_http_method,
        FakeConfig,
    ):
        """POST or DELETE to ua-contracts and write machine-token cache.

        Setting detach=True will result in a DELETE operation.
        """
        get_platform_info.return_value = {"arch": "arch", "kernel": "kernel"}
        get_machine_id.return_value = "machineId"
        request_url.return_value = ("newtoken", {})
        cfg = FakeConfig.for_attached_machine()
        client = UAContractClient(cfg)
        kwargs = {"machine_token": "mToken", "contract_id": "cId"}
        if detach is not None:
            kwargs["detach"] = detach
        client._request_machine_token_update(**kwargs)
        if not detach:  # Then we have written the updated cache
            assert "newtoken" == cfg.read_cache("machine-token")
        params = {
            "headers": {
                "user-agent": "UA-Client/{}".format(get_version()),
                "accept": "application/json",
                "content-type": "application/json",
                "Authorization": "Bearer mToken",
            },
            "method": expected_http_method,
        }
        if expected_http_method != "DELETE":
            params["data"] = {
                "machineId": "machineId",
                "architecture": "arch",
                "os": {"kernel": "kernel"},
            }
        assert [
            mock.call("/v1/contracts/cId/context/machines/machineId", **params)
        ] == request_url.call_args_list

    def test_request_resource_machine_access(
        self, get_machine_id, request_url, FakeConfig
    ):
        """GET from resource-machine-access route to "enable" a service"""
        get_machine_id.return_value = "machineId"
        request_url.return_value = ("response", {})
        cfg = FakeConfig.for_attached_machine()
        client = UAContractClient(cfg)
        kwargs = {"machine_token": "mToken", "resource": "cis"}
        assert "response" == client.request_resource_machine_access(**kwargs)
        assert "response" == cfg.read_cache("machine-access-cis")
        params = {
            "headers": {
                "user-agent": "UA-Client/{}".format(get_version()),
                "accept": "application/json",
                "content-type": "application/json",
                "Authorization": "Bearer mToken",
            }
        }
        assert [
            mock.call("/v1/resources/cis/context/machines/machineId", **params)
        ] == request_url.call_args_list


class TestProcessEntitlementDeltas:
    def test_error_on_missing_entitlement_type(self):
        """Raise an error when neither dict contains entitlement type."""
        new_access = {"entitlement": {"something": "non-empty"}}
        error_msg = (
            "Could not determine contract delta service type"
            " {{}} {}".format(new_access)
        )
        with pytest.raises(RuntimeError) as exc:
            process_entitlement_delta({}, new_access)
        assert error_msg == str(exc.value)

    def test_no_delta_on_equal_dicts(self):
        """No deltas are reported or processed when dicts are equal."""
        assert {} == process_entitlement_delta(
            {"entitlement": {"no": "diff"}}, {"entitlement": {"no": "diff"}}
        )

    @mock.patch(M_REPO_PATH + "process_contract_deltas")
    def test_deltas_handled_by_entitlement_process_contract_deltas(
        self, m_process_contract_deltas
    ):
        """Call entitlement.process_contract_deltas to handle any deltas."""
        original_access = {"entitlement": {"type": "esm-infra"}}
        new_access = copy.deepcopy(original_access)
        new_access["entitlement"]["newkey"] = "newvalue"
        expected = {"entitlement": {"newkey": "newvalue"}}
        assert expected == process_entitlement_delta(
            original_access, new_access
        )
        expected_calls = [
            mock.call(original_access, expected, allow_enable=False)
        ]
        assert expected_calls == m_process_contract_deltas.call_args_list

    @mock.patch(M_REPO_PATH + "process_contract_deltas")
    def test_full_delta_on_empty_orig_dict(self, m_process_contract_deltas):
        """Process and report full deltas on empty original access dict."""
        # Limit delta processing logic to handle attached state-A to state-B
        # Fresh installs will have empty/unset
        new_access = {"entitlement": {"type": "esm-infra", "other": "val2"}}
        assert new_access == process_entitlement_delta({}, new_access)
        expected_calls = [mock.call({}, new_access, allow_enable=False)]
        assert expected_calls == m_process_contract_deltas.call_args_list

    @mock.patch(
        "uaclient.util.get_platform_info",
        return_value={"series": "fake_series"},
    )
    @mock.patch(M_REPO_PATH + "process_contract_deltas")
    def test_overrides_applied_before_comparison(
        self, m_process_contract_deltas, _
    ):
        old_access = {"entitlement": {"type": "esm", "some_key": "some_value"}}
        new_access = {
            "entitlement": {
                "type": "esm",
                "some_key": "will be overridden",
                "series": {"fake_series": {"some_key": "some_value"}},
            }
        }

        process_entitlement_delta(old_access, new_access)

        assert 0 == m_process_contract_deltas.call_count


class TestGetAvailableResources:
    @mock.patch.object(UAContractClient, "request_resources")
    def test_request_resources_error_on_network_disconnected(
        self, m_request_resources, FakeConfig
    ):
        """Raise error get_available_resources can't contact backend"""
        cfg = FakeConfig()

        urlerror = util.UrlError(
            socket.gaierror(-2, "Name or service not known")
        )
        m_request_resources.side_effect = urlerror

        with pytest.raises(util.UrlError) as exc:
            get_available_resources(cfg)
        assert urlerror == exc.value

    @mock.patch(M_PATH + "UAContractClient")
    def test_request_resources_from_contract_server(self, client, FakeConfig):
        """Call UAContractClient.request_resources to get updated resources."""
        cfg = FakeConfig()

        url = API_V1_RESOURCES

        new_resources = [{"name": "new_resource", "available": False}]

        def fake_contract_client(cfg):
            fake_client = FakeContractClient(cfg)
            fake_client._responses = {url: {"resources": new_resources}}
            return fake_client

        client.side_effect = fake_contract_client
        assert new_resources == get_available_resources(cfg)


class TestRequestUpdatedContract:

    refresh_route = API_V1_TMPL_CONTEXT_MACHINE_TOKEN_RESOURCE.format(
        contract="cid", machine="mid"
    )
    access_route_ent1 = API_V1_TMPL_RESOURCE_MACHINE_ACCESS.format(
        resource="ent1", machine="mid"
    )
    access_route_ent2 = API_V1_TMPL_RESOURCE_MACHINE_ACCESS.format(
        resource="ent2", machine="mid"
    )

    @mock.patch(M_PATH + "UAContractClient")
    def test_attached_config_and_contract_token_runtime_error(
        self, client, FakeConfig
    ):
        """When attached, error if called with a contract_token."""

        def fake_contract_client(cfg):
            return FakeContractClient(cfg)

        client.side_effect = fake_contract_client
        cfg = FakeConfig.for_attached_machine()
        with pytest.raises(RuntimeError) as exc:
            request_updated_contract(cfg, contract_token="something")

        expected_msg = (
            "Got unexpected contract_token on an already attached machine"
        )
        assert expected_msg == str(exc.value)

    @pytest.mark.parametrize(
        "error_code, error_msg, error_response",
        (
            (401, MESSAGE_ATTACH_INVALID_TOKEN, '{"message": "unauthorized"}'),
            (403, MESSAGE_ATTACH_EXPIRED_TOKEN, "{}"),
            (
                403,
                MESSAGE_ATTACH_FORBIDDEN.format(
                    reason=MESSAGE_ATTACH_FORBIDDEN_EXPIRED.format(
                        contract_id="contract-id", date="May 07, 2021"
                    )
                ),
                """{
                "code": "forbidden",
                "info": {
                    "contractId": "contract-id",
                    "reason": "no-longer-effective",
                    "time": "2021-05-07T09:46:37.791Z"
                },
                "message": "contract \\"contract-id\\" is no longer effective",
                "traceId": "7f58c084-f753-455d-9bdc-65b839d6536f"
                }""",
            ),
            (
                403,
                MESSAGE_ATTACH_FORBIDDEN.format(
                    reason=MESSAGE_ATTACH_FORBIDDEN_NOT_YET.format(
                        contract_id="contract-id", date="May 07, 2021"
                    )
                ),
                """{
                "code": "forbidden",
                "info": {
                    "contractId": "contract-id",
                    "reason": "not-effective-yet",
                    "time": "2021-05-07T09:46:37.791Z"
                },
                "message": "contract \\"contract-id\\" is not effective yet",
                "traceId": "7f58c084-f753-455d-9bdc-65b839d6536f"
                }""",
            ),
            (
                403,
                MESSAGE_ATTACH_FORBIDDEN.format(
                    reason=MESSAGE_ATTACH_FORBIDDEN_NEVER.format(
                        contract_id="contract-id"
                    )
                ),
                """{
                "code": "forbidden",
                "info": {
                    "contractId": "contract-id",
                    "reason": "never-effective"
                },
                "message": "contract \\"contract-id\\" is was never effective",
                "traceId": "7f58c084-f753-455d-9bdc-65b839d6536f"
            }""",
            ),
        ),
    )
    @mock.patch("uaclient.util.get_machine_id", return_value="mid")
    @mock.patch(M_PATH + "UAContractClient")
    def test_invalid_token_user_facing_error_on_invalid_token_refresh_failure(
        self,
        client,
        get_machine_id,
        FakeConfig,
        error_code,
        error_msg,
        error_response,
    ):
        """When attaching, invalid token errors result in proper user error."""

        def fake_contract_client(cfg):
            fake_client = FakeContractClient(cfg)
            fake_client._responses = {
                API_V1_CONTEXT_MACHINE_TOKEN: ContractAPIError(
                    util.UrlError(
                        "Server error",
                        code=error_code,
                        url="http://me",
                        headers={},
                    ),
                    error_response=json.loads(
                        error_response, cls=util.DatetimeAwareJSONDecoder
                    ),
                )
            }
            return fake_client

        client.side_effect = fake_contract_client
        cfg = FakeConfig()
        with pytest.raises(exceptions.UserFacingError) as exc:
            request_updated_contract(cfg, contract_token="yep")

        assert error_msg == str(exc.value)

    @mock.patch("uaclient.util.get_machine_id", return_value="mid")
    @mock.patch(M_PATH + "UAContractClient")
    def test_user_facing_error_on_machine_token_refresh_failure(
        self, client, get_machine_id, FakeConfig
    ):
        """When attaching, error on failure to refresh the machine token."""

        def fake_contract_client(cfg):
            fake_client = FakeContractClient(cfg)
            fake_client._responses = {
                self.refresh_route: exceptions.UserFacingError(
                    "Machine token refresh fail"
                )
            }
            return fake_client

        client.side_effect = fake_contract_client
        cfg = FakeConfig.for_attached_machine()
        with pytest.raises(exceptions.UserFacingError) as exc:
            request_updated_contract(cfg)

        assert "Machine token refresh fail" == str(exc.value)

    @mock.patch("uaclient.util.get_machine_id", return_value="mid")
    @mock.patch(M_PATH + "UAContractClient")
    def test_user_facing_error_on_service_token_refresh_failure(
        self, client, get_machine_id, FakeConfig
    ):
        """When attaching, error on any failed specific service refresh."""

        machine_token = {
            "machineToken": "mToken",
            "machineTokenInfo": {
                "contractInfo": {
                    "id": "cid",
                    "resourceEntitlements": [
                        {"entitled": True, "type": "ent2"},
                        {"entitled": True, "type": "ent1"},
                    ],
                }
            },
        }

        def fake_contract_client(cfg):
            fake_client = FakeContractClient(cfg)
            fake_client._responses = {self.refresh_route: machine_token}
            return fake_client

        client.side_effect = fake_contract_client
        cfg = FakeConfig.for_attached_machine(machine_token=machine_token)
        with mock.patch(M_PATH + "process_entitlement_delta") as m_process:
            m_process.side_effect = (
                exceptions.UserFacingError("broken ent1"),
                exceptions.UserFacingError("broken ent2"),
            )
            with pytest.raises(exceptions.UserFacingError) as exc:
                request_updated_contract(cfg)

        assert MESSAGE_ATTACH_FAILURE_DEFAULT_SERVICES == str(exc.value)

    @pytest.mark.parametrize(
        "first_error, second_error, ux_error_msg",
        (
            (
                exceptions.UserFacingError(
                    "Ubuntu Advantage server provided no aptKey directive for"
                    " esm-infra"
                ),
                None,
                MESSAGE_ATTACH_FAILURE_DEFAULT_SERVICES,
            ),
            (RuntimeError("some APT error"), None, MESSAGE_UNEXPECTED_ERROR),
            # Order high-priority RuntimeError as second_error to ensure it
            # is raised as primary error_msg
            (
                exceptions.UserFacingError(
                    "Ubuntu Advantage server provided no aptKey directive for"
                    " esm-infra"
                ),
                RuntimeError("some APT error"),  # High-priority ordered 2
                MESSAGE_UNEXPECTED_ERROR,
            ),
        ),
    )
    @mock.patch(M_PATH + "process_entitlement_delta")
    @mock.patch("uaclient.util.get_machine_id", return_value="mid")
    @mock.patch(M_PATH + "UAContractClient")
    def test_user_facing_error_due_to_unexpected_process_entitlement_delta(
        self,
        client,
        get_machine_id,
        process_entitlement_delta,
        first_error,
        second_error,
        ux_error_msg,
        FakeConfig,
    ):
        """Unexpected errors from process_entitlement_delta are raised.

        Remaining entitlements are processed regardless of error and error is
        raised at the end.

        Unexpected exceptions take priority over the handled UserFacingErrors.
        """
        # Fail first and succeed second call to process_entitlement_delta
        process_entitlement_delta.side_effect = (
            first_error,
            second_error,
            None,
        )

        # resourceEntitlements specifically ordered reverse alphabetically
        # to ensure proper sorting for process_contract_delta calls below
        machine_token = {
            "machineToken": "mToken",
            "machineTokenInfo": {
                "contractInfo": {
                    "id": "cid",
                    "resourceEntitlements": [
                        {"entitled": False, "type": "ent3"},
                        {"entitled": False, "type": "ent2"},
                        {"entitled": True, "type": "ent1"},
                    ],
                }
            },
        }

        cfg = FakeConfig.for_attached_machine(machine_token=machine_token)
        fake_client = FakeContractClient(cfg)
        fake_client._responses = {
            self.refresh_route: machine_token,
            self.access_route_ent1: {
                "entitlement": {
                    "entitled": True,
                    "type": "ent1",
                    "new": "newval",
                }
            },
        }

        client.return_value = fake_client
        with pytest.raises(exceptions.UserFacingError) as exc:
            assert None is request_updated_contract(cfg)
        assert 3 == process_entitlement_delta.call_count
        assert ux_error_msg == str(exc.value)

    @mock.patch(M_PATH + "process_entitlement_delta")
    @mock.patch("uaclient.util.get_machine_id", return_value="mid")
    @mock.patch(M_PATH + "UAContractClient")
    def test_attached_config_refresh_machine_token_and_services(
        self, client, get_machine_id, process_entitlement_delta, FakeConfig
    ):
        """When attached, refresh machine token and entitled services.

        Processing service deltas are processed in a sorted order based on
        name to ensure operations occur the same regardless of dict ordering.
        """

        # resourceEntitlements specifically ordered reverse alphabetically
        # to ensure proper sorting for process_contract_delta calls below
        machine_token = {
            "machineToken": "mToken",
            "machineTokenInfo": {
                "contractInfo": {
                    "id": "cid",
                    "resourceEntitlements": [
                        {"entitled": False, "type": "ent2"},
                        {"entitled": True, "type": "ent1"},
                    ],
                }
            },
        }
        new_token = copy.deepcopy(machine_token)
        new_token["machineTokenInfo"]["contractInfo"]["resourceEntitlements"][
            1
        ]["new"] = "newval"

        def fake_contract_client(cfg):
            client = FakeContractClient(cfg)
            client._responses = {self.refresh_route: new_token}
            return client

        client.side_effect = fake_contract_client
        cfg = FakeConfig.for_attached_machine(machine_token=machine_token)
        assert None is request_updated_contract(cfg)
        assert new_token == cfg.read_cache("machine-token")

        # Deltas are processed in a sorted fashion so that if enableByDefault
        # is true, the order of enablement operations is the same regardless
        # of dict key ordering.
        process_calls = [
            mock.call(
                {"entitlement": {"entitled": True, "type": "ent1"}},
                {
                    "entitlement": {
                        "entitled": True,
                        "type": "ent1",
                        "new": "newval",
                    }
                },
                allow_enable=False,
                series_overrides=True,
            ),
            mock.call(
                {"entitlement": {"entitled": False, "type": "ent2"}},
                {"entitlement": {"entitled": False, "type": "ent2"}},
                allow_enable=False,
                series_overrides=True,
            ),
        ]
        assert process_calls == process_entitlement_delta.call_args_list


class TestDetachMachineFromContract:
    @pytest.mark.parametrize("caplog_text", [logging.DEBUG], indirect=True)
    @pytest.mark.parametrize(
        "curr_machine_id,past_machine_id",
        (("123", "124"), (123, "124"), ("123", 124), ("123", "123")),
    )
    @mock.patch.object(UAContractClient, "_request_machine_token_update")
    @mock.patch.object(UAContractClient, "_get_platform_data")
    def test_do_not_make_make_detach_call_when_machine_id_is_different(
        self,
        m_platform_data,
        m_request_machine_token_update,
        curr_machine_id,
        past_machine_id,
        caplog_text,
        FakeConfig,
    ):
        m_platform_data.return_value = {"machineId": curr_machine_id}
        cfg = FakeConfig.for_attached_machine()
        cfg.write_cache("machine-id", past_machine_id)
        client = UAContractClient(cfg)

        actual_value = client.detach_machine_from_contract(
            machine_token="machine_token",
            contract_id="contract_id",
            machine_id="machine_id",
        )

        expected_msg = """\
        Found new machine-id. Do not call detach on contract backend
        """
        if str(past_machine_id) != str(curr_machine_id):
            assert actual_value == {}
            assert m_request_machine_token_update.call_count == 0
            assert expected_msg.strip() in caplog_text()
        else:
            assert m_request_machine_token_update.call_count == 1
            assert expected_msg.strip() not in caplog_text()
