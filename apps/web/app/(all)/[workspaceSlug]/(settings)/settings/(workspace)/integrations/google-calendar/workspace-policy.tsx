/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
import useSWR from "swr";
// plane imports
import { GOOGLE_CALENDAR_STATUS } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { IGoogleCalendarWorkspacePolicy, IGoogleCalendarWorkspaceStatus } from "@plane/types";
import { EUserWorkspaceRoles } from "@plane/types";
import { AlertModalCore, CustomSelect, Loader, ToggleSwitch } from "@plane/ui";
// components
import { SettingsBoxedControlItem } from "@/components/settings/boxed-control-item";
// hooks
import { useUserPermissions } from "@/hooks/store/user";
// services
import { IntegrationService } from "@/services/integrations";

type Props = {
  workspaceSlug: string;
};

type TPolicyMutation = "completion" | "enabled" | null;

const integrationService = new IntegrationService();

const getPolicyErrorCode = (error: unknown): string | null => {
  if (typeof error !== "object" || error === null || !("error" in error)) return null;

  const errorCode = (error as { error?: unknown }).error;
  return typeof errorCode === "string" ? errorCode : null;
};

export const GoogleCalendarWorkspacePolicy = observer(function GoogleCalendarWorkspacePolicy({ workspaceSlug }: Props) {
  // states
  const [isDisableConfirmationOpen, setIsDisableConfirmationOpen] = useState(false);
  const [policyMutation, setPolicyMutation] = useState<TPolicyMutation>(null);
  // store hooks
  const { getWorkspaceRoleByWorkspaceSlug } = useUserPermissions();
  // translation
  const { t } = useTranslation();
  // derived values
  const isAdmin = getWorkspaceRoleByWorkspaceSlug(workspaceSlug) === EUserWorkspaceRoles.ADMIN;

  const {
    data: workspaceStatus,
    error: workspaceStatusError,
    mutate: mutateStatus,
  } = useSWR<IGoogleCalendarWorkspaceStatus>(isAdmin ? GOOGLE_CALENDAR_STATUS(workspaceSlug) : null, () =>
    integrationService.getGoogleCalendarStatus(workspaceSlug)
  );

  if (!isAdmin) return null;

  const updatePolicy = async (
    policy: IGoogleCalendarWorkspacePolicy,
    mutation: Exclude<TPolicyMutation, null>
  ): Promise<boolean> => {
    setPolicyMutation(mutation);

    try {
      const updatedPolicy = await integrationService.updateGoogleCalendarPolicy(workspaceSlug, policy);
      await mutateStatus(
        (status) =>
          status
            ? {
                ...status,
                policy: updatedPolicy,
                connection:
                  !updatedPolicy.enabled && status.connection ? { status: "disconnecting" } : status.connection,
              }
            : status,
        { revalidate: false }
      );
      void mutateStatus();
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: t("workspace_settings.settings.integrations.google_calendar.workspace_policy.toast.success.title"),
        message: t("workspace_settings.settings.integrations.google_calendar.workspace_policy.toast.success.message"),
      });
      return true;
    } catch (error) {
      const errorCode = getPolicyErrorCode(error);
      const messageKey =
        errorCode === "google_calendar_credentials_incomplete"
          ? "workspace_settings.settings.integrations.google_calendar.workspace_policy.toast.error.credentials"
          : errorCode === "google_calendar_disable_cleanup_in_progress"
            ? "workspace_settings.settings.integrations.google_calendar.workspace_policy.toast.error.cleanup"
            : "workspace_settings.settings.integrations.google_calendar.workspace_policy.toast.error.message";

      setToast({
        type: TOAST_TYPE.ERROR,
        title: t("workspace_settings.settings.integrations.google_calendar.workspace_policy.toast.error.title"),
        message: t(messageKey),
      });
      return false;
    } finally {
      setPolicyMutation(null);
    }
  };

  const handleEnabledChange = async (enabled: boolean) => {
    if (!workspaceStatus || enabled === workspaceStatus.policy.enabled) return;

    if (!enabled) {
      setIsDisableConfirmationOpen(true);
      return;
    }

    await updatePolicy({ ...workspaceStatus.policy, enabled }, "enabled");
  };

  const handleCompletionChange = async (updateOnCompletion: boolean) => {
    if (!workspaceStatus || updateOnCompletion === workspaceStatus.policy.update_on_completion) return;

    await updatePolicy({ ...workspaceStatus.policy, update_on_completion: updateOnCompletion }, "completion");
  };

  const handleDisable = async () => {
    if (!workspaceStatus) return;

    const wasUpdated = await updatePolicy({ ...workspaceStatus.policy, enabled: false }, "enabled");
    if (wasUpdated) setIsDisableConfirmationOpen(false);
  };

  if (!workspaceStatus && !workspaceStatusError)
    return (
      <Loader className="space-y-3 rounded-lg border border-subtle p-4">
        <Loader.Item height="20px" width="30%" />
        <Loader.Item height="48px" width="100%" />
        <Loader.Item height="48px" width="100%" />
      </Loader>
    );

  if (!workspaceStatus) return null;

  const isMutating = policyMutation !== null;

  return (
    <>
      <section className="flex flex-col gap-4">
        <div className="flex flex-col gap-1">
          <h3 className="text-body-md-semibold text-primary">
            {t("workspace_settings.settings.integrations.google_calendar.workspace_policy.title")}
          </h3>
          <p className="text-body-sm-regular text-secondary">
            {t("workspace_settings.settings.integrations.google_calendar.workspace_policy.description")}
          </p>
        </div>

        <div className="flex flex-col gap-3">
          <SettingsBoxedControlItem
            title={t("workspace_settings.settings.integrations.google_calendar.workspace_policy.enabled.title")}
            description={t(
              "workspace_settings.settings.integrations.google_calendar.workspace_policy.enabled.description"
            )}
            control={
              <ToggleSwitch
                value={workspaceStatus.policy.enabled}
                onChange={(enabled) => void handleEnabledChange(enabled)}
                label={t("workspace_settings.settings.integrations.google_calendar.workspace_policy.enabled.label")}
                disabled={isMutating || (!workspaceStatus.available && !workspaceStatus.policy.enabled)}
                size="sm"
              />
            }
          />
          <SettingsBoxedControlItem
            title={t("workspace_settings.settings.integrations.google_calendar.workspace_policy.completion.title")}
            description={t(
              "workspace_settings.settings.integrations.google_calendar.workspace_policy.completion.description"
            )}
            control={
              <CustomSelect
                value={workspaceStatus.policy.update_on_completion}
                onChange={(updateOnCompletion: boolean) => void handleCompletionChange(updateOnCompletion)}
                label={t(
                  workspaceStatus.policy.update_on_completion
                    ? "workspace_settings.settings.integrations.google_calendar.workspace_policy.completion.options.update"
                    : "workspace_settings.settings.integrations.google_calendar.workspace_policy.completion.options.delete"
                )}
                disabled={isMutating}
                buttonClassName="min-w-44 border border-subtle-1"
                input
                placement="bottom-end"
              >
                <CustomSelect.Option value={true}>
                  {t(
                    "workspace_settings.settings.integrations.google_calendar.workspace_policy.completion.options.update"
                  )}
                </CustomSelect.Option>
                <CustomSelect.Option value={false}>
                  {t(
                    "workspace_settings.settings.integrations.google_calendar.workspace_policy.completion.options.delete"
                  )}
                </CustomSelect.Option>
              </CustomSelect>
            }
          />
        </div>
      </section>

      <AlertModalCore
        isOpen={isDisableConfirmationOpen}
        handleClose={() => setIsDisableConfirmationOpen(false)}
        handleSubmit={() => void handleDisable()}
        isSubmitting={policyMutation === "enabled"}
        title={t("workspace_settings.settings.integrations.google_calendar.workspace_policy.disable.title")}
        content={t("workspace_settings.settings.integrations.google_calendar.workspace_policy.disable.description")}
        primaryButtonText={{
          default: t("workspace_settings.settings.integrations.google_calendar.workspace_policy.disable.confirm"),
          loading: t("workspace_settings.settings.integrations.google_calendar.workspace_policy.disable.disabling"),
        }}
        secondaryButtonText={t(
          "workspace_settings.settings.integrations.google_calendar.workspace_policy.disable.cancel"
        )}
      />
    </>
  );
});
