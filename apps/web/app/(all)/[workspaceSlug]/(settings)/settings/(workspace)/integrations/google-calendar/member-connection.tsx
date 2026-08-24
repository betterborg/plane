/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2 } from "lucide-react";
import { observer } from "mobx-react";
import { useSearchParams } from "next/navigation";
import useSWR, { mutate as mutateSWR } from "swr";
// plane imports
import { API_BASE_URL, GOOGLE_CALENDAR_CONNECTION_ROSTER, GOOGLE_CALENDAR_STATUS } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { Banner } from "@plane/propel/banner";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { IGoogleCalendarWorkspaceStatus, TGoogleCalendarConnectionStatus } from "@plane/types";
import { EUserWorkspaceRoles } from "@plane/types";
import { Loader } from "@plane/ui";
// hooks
import useIntegrationPopup from "@/hooks/use-integration-popup";
import { useUser, useUserPermissions } from "@/hooks/store/user";
// services
import { IntegrationService } from "@/services/integrations";
// local imports
import { GoogleCalendarConnectionStatus } from "./connection-status";

type Props = {
  workspaceSlug: string;
};

type TCallbackResult = string | null;

const integrationService = new IntegrationService();

const ACTIVE_LIFECYCLE_STATUSES = new Set<TGoogleCalendarConnectionStatus>(["provisioning", "disconnecting"]);

const getCallbackResult = (searchParams: URLSearchParams): TCallbackResult => {
  const error = searchParams.get("error");
  if (error) return error;
  return searchParams.get("google_calendar_oauth") === "success" ? "success" : null;
};

const getCallbackResultFromUrl = (callbackUrl?: string): TCallbackResult => {
  if (!callbackUrl) return null;

  try {
    return getCallbackResult(new URL(callbackUrl).searchParams);
  } catch {
    return null;
  }
};

const CallbackResultBanner = ({ result }: { result: TCallbackResult }) => {
  const { t } = useTranslation();

  if (!result) return null;

  if (result === "account_switch_requires_disconnect")
    return (
      <Banner
        variant="warning"
        icon={<AlertCircle className="size-4" />}
        title={
          <div className="flex flex-col gap-2">
            <p>
              {t("workspace_settings.settings.integrations.google_calendar.member_connection.callback.account_switch")}
            </p>
            <ol className="list-decimal space-y-1 pl-4 text-caption-md-regular">
              <li>
                {t(
                  "workspace_settings.settings.integrations.google_calendar.member_connection.callback.account_switch_steps.reconnect"
                )}
              </li>
              <li>
                {t(
                  "workspace_settings.settings.integrations.google_calendar.member_connection.callback.account_switch_steps.disconnect"
                )}
              </li>
              <li>
                {t(
                  "workspace_settings.settings.integrations.google_calendar.member_connection.callback.account_switch_steps.connect"
                )}
              </li>
            </ol>
          </div>
        }
      />
    );

  if (result === "success")
    return (
      <Banner
        variant="success"
        icon={<CheckCircle2 className="size-4" />}
        title={t("workspace_settings.settings.integrations.google_calendar.member_connection.callback.success")}
      />
    );

  const isDisabled = result === "google_calendar_disabled";
  const isStale = ["google_calendar_oauth_invalid_state", "google_calendar_oauth_stale"].includes(result);
  const messageKey = isDisabled
    ? "workspace_settings.settings.integrations.google_calendar.member_connection.callback.disabled"
    : isStale
      ? "workspace_settings.settings.integrations.google_calendar.member_connection.callback.stale"
      : "workspace_settings.settings.integrations.google_calendar.member_connection.callback.failed";

  return <Banner variant="error" icon={<AlertCircle className="size-4" />} title={t(messageKey)} />;
};

export const GoogleCalendarMemberConnection = observer(function GoogleCalendarMemberConnection({
  workspaceSlug,
}: Props) {
  // router
  const searchParams = useSearchParams();
  // store hooks
  const { data: currentUser } = useUser();
  const { getWorkspaceRoleByWorkspaceSlug } = useUserPermissions();
  // translation
  const { t } = useTranslation();
  // states
  const [callbackResult, setCallbackResult] = useState<TCallbackResult>(() => getCallbackResult(searchParams));
  const [isDisconnecting, setIsDisconnecting] = useState(false);
  // derived values
  const isAdmin = getWorkspaceRoleByWorkspaceSlug(workspaceSlug) === EUserWorkspaceRoles.ADMIN;

  const {
    data: workspaceStatus,
    error: workspaceStatusError,
    isLoading,
    mutate: mutateStatus,
  } = useSWR<IGoogleCalendarWorkspaceStatus>(
    GOOGLE_CALENDAR_STATUS(workspaceSlug),
    () => integrationService.getGoogleCalendarStatus(workspaceSlug),
    {
      refreshInterval: (status) =>
        status?.connection && ACTIVE_LIFECYCLE_STATUSES.has(status.connection.status) ? 3000 : 0,
    }
  );

  const handlePopupClose = useCallback(
    (callbackUrl?: string) => {
      const popupResult = getCallbackResultFromUrl(callbackUrl);
      if (popupResult) setCallbackResult(popupResult);

      const statusRevalidation = mutateStatus();
      const rosterRevalidation = isAdmin
        ? mutateSWR(GOOGLE_CALENDAR_CONNECTION_ROSTER(workspaceSlug))
        : Promise.resolve();
      void Promise.all([statusRevalidation, rosterRevalidation]).then(([refreshedStatus]) => {
        if (!popupResult && refreshedStatus && !refreshedStatus.policy.enabled)
          setCallbackResult("google_calendar_disabled");
        return refreshedStatus;
      });
    },
    [isAdmin, mutateStatus, workspaceSlug]
  );

  const authUrl = useMemo(
    () =>
      `${API_BASE_URL}/api/workspaces/${encodeURIComponent(workspaceSlug)}/integrations/google-calendar/oauth/start/`,
    [workspaceSlug]
  );
  const { isConnecting, startAuth } = useIntegrationPopup({ authUrl, onClose: handlePopupClose });

  const handleDisconnect = async () => {
    if (!currentUser?.id) return;

    setIsDisconnecting(true);
    try {
      await integrationService.disconnectGoogleCalendarConnection(workspaceSlug, currentUser.id);
      await mutateStatus((status) => (status ? { ...status, connection: { status: "disconnecting" } } : status), {
        revalidate: true,
      });
      if (isAdmin) await mutateSWR(GOOGLE_CALENDAR_CONNECTION_ROSTER(workspaceSlug));
    } catch {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: t("workspace_settings.settings.integrations.google_calendar.member_connection.disconnect_error.title"),
        message: t(
          "workspace_settings.settings.integrations.google_calendar.member_connection.disconnect_error.message"
        ),
      });
    } finally {
      setIsDisconnecting(false);
    }
  };

  if (isLoading && !workspaceStatus)
    return (
      <Loader className="space-y-3 rounded-lg border border-subtle p-4">
        <Loader.Item height="20px" width="30%" />
        <Loader.Item height="16px" width="75%" />
        <Loader.Item height="32px" width="20%" />
      </Loader>
    );

  if (workspaceStatusError || !workspaceStatus)
    return (
      <Banner
        variant="error"
        icon={<AlertCircle className="size-4" />}
        title={t("workspace_settings.settings.integrations.google_calendar.member_connection.load_error")}
        action={
          <Button variant="secondary" onClick={() => void mutateStatus()}>
            {t("workspace_settings.settings.integrations.google_calendar.member_connection.retry")}
          </Button>
        }
      />
    );

  const connectionStatus = workspaceStatus.connection?.status ?? null;
  const connectionError = searchParams.get("connection_error");
  const isCredentialMismatch = connectionStatus === "broken" && connectionError === "oauth_credentials_changed";
  const canStartAuth =
    workspaceStatus.available && workspaceStatus.policy.enabled && connectionStatus !== "disconnecting";
  const hasConnection = connectionStatus !== null;

  return (
    <div className="flex flex-col gap-4">
      <CallbackResultBanner result={callbackResult} />

      {!workspaceStatus.policy.enabled && (
        <Banner
          variant="info"
          title={t("workspace_settings.settings.integrations.google_calendar.member_connection.policy_disabled")}
        />
      )}
      {!workspaceStatus.available && (
        <Banner
          variant="warning"
          title={t("workspace_settings.settings.integrations.google_calendar.member_connection.unavailable")}
        />
      )}
      {connectionStatus === "broken" && (
        <Banner
          variant="error"
          icon={<AlertCircle className="size-4" />}
          title={t(
            isCredentialMismatch
              ? "workspace_settings.settings.integrations.google_calendar.member_connection.guidance.credentials_changed"
              : "workspace_settings.settings.integrations.google_calendar.member_connection.guidance.broken"
          )}
        />
      )}

      <section className="flex flex-col gap-4 rounded-lg border border-subtle bg-layer-2 px-4 py-4">
        <div className="flex flex-col gap-1">
          <h3 className="text-body-md-semibold text-primary">
            {t("workspace_settings.settings.integrations.google_calendar.member_connection.title")}
          </h3>
          <p className="max-w-3xl text-body-sm-regular text-secondary">
            {t("workspace_settings.settings.integrations.google_calendar.member_connection.consent")}
          </p>
        </div>

        <div className="flex flex-col items-start justify-between gap-3 border-t border-subtle pt-4 sm:flex-row sm:items-center">
          <GoogleCalendarConnectionStatus status={connectionStatus} />
          <div className="flex items-center gap-2">
            {connectionStatus === "disconnecting" ? (
              <Button variant="secondary" disabled>
                {t("workspace_settings.settings.integrations.google_calendar.member_connection.actions.disconnecting")}
              </Button>
            ) : (
              <>
                <Button variant="primary" onClick={startAuth} loading={isConnecting} disabled={!canStartAuth}>
                  {t(
                    hasConnection
                      ? "workspace_settings.settings.integrations.google_calendar.member_connection.actions.reconnect"
                      : "workspace_settings.settings.integrations.google_calendar.member_connection.actions.connect"
                  )}
                </Button>
                {hasConnection && (
                  <Button
                    variant="error-outline"
                    onClick={() => void handleDisconnect()}
                    loading={isDisconnecting}
                    disabled={!currentUser?.id}
                  >
                    {t("workspace_settings.settings.integrations.google_calendar.member_connection.actions.disconnect")}
                  </Button>
                )}
              </>
            )}
          </div>
        </div>
      </section>
    </div>
  );
});
