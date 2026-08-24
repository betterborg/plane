/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect, useState } from "react";
import { observer } from "mobx-react";
import useSWR from "swr";
// plane imports
import { GOOGLE_CALENDAR_FILTER_OPTIONS, GOOGLE_CALENDAR_STATUS, ISSUE_PRIORITIES } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type {
  IGoogleCalendarFilterOptions,
  IGoogleCalendarWorkspacePolicy,
  IGoogleCalendarWorkspaceStatus,
  TIssuePriorities,
} from "@plane/types";
import { EUserWorkspaceRoles } from "@plane/types";
import { AlertModalCore, CustomSelect, Loader, MultiSelectDropdown, ToggleSwitch } from "@plane/ui";
// components
import { SettingsBoxedControlItem } from "@/components/settings/boxed-control-item";
// hooks
import { useUserPermissions } from "@/hooks/store/user";
// services
import { IntegrationService } from "@/services/integrations";

type Props = {
  workspaceSlug: string;
};

type TFilterPolicyDraft = Pick<IGoogleCalendarWorkspacePolicy, "label_ids" | "label_match" | "mode" | "priorities">;

type TPolicyMutation = "completion" | "enabled" | "filter" | null;

const integrationService = new IntegrationService();

const isIssuePriority = (value: string): value is TIssuePriorities =>
  ISSUE_PRIORITIES.some((priority) => priority.key === value);

const getFilterPolicy = (policy: IGoogleCalendarWorkspacePolicy): TFilterPolicyDraft => ({
  mode: policy.mode,
  label_ids: policy.label_ids,
  priorities: policy.priorities,
  label_match: policy.label_match,
});

const getAvailableFilterPolicy = (
  policy: TFilterPolicyDraft,
  options: IGoogleCalendarFilterOptions | undefined
): TFilterPolicyDraft => {
  if (!options) return policy;

  const availableLabelIds = new Set(options.labels.map((label) => label.id));
  const availablePriorities = new Set(options.priorities.map((priority) => priority.key));
  return {
    ...policy,
    label_ids: policy.label_ids.filter((labelId) => availableLabelIds.has(labelId)),
    priorities: policy.priorities.filter((priority) => availablePriorities.has(priority)),
  };
};

const getPolicyErrorCode = (error: unknown): string | null => {
  if (typeof error !== "object" || error === null || !("error" in error)) return null;

  const errorCode = (error as { error?: unknown }).error;
  return typeof errorCode === "string" ? errorCode : null;
};

export const GoogleCalendarWorkspacePolicy = observer(function GoogleCalendarWorkspacePolicy({ workspaceSlug }: Props) {
  // states
  const [isDisableConfirmationOpen, setIsDisableConfirmationOpen] = useState(false);
  const [policyMutation, setPolicyMutation] = useState<TPolicyMutation>(null);
  const [filterPolicyDraft, setFilterPolicyDraft] = useState<TFilterPolicyDraft | null>(null);
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
  const {
    data: filterOptions,
    error: filterOptionsError,
    mutate: mutateFilterOptions,
  } = useSWR<IGoogleCalendarFilterOptions>(isAdmin ? GOOGLE_CALENDAR_FILTER_OPTIONS(workspaceSlug) : null, () =>
    integrationService.getGoogleCalendarFilterOptions(workspaceSlug)
  );

  useEffect(() => {
    setFilterPolicyDraft(null);
  }, [workspaceSlug]);

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

  const handleFilterSave = async () => {
    if (!workspaceStatus) return;

    const filterPolicy = getAvailableFilterPolicy(
      filterPolicyDraft ?? getFilterPolicy(workspaceStatus.policy),
      filterOptions
    );
    if (filterPolicy.mode === "filter" && filterPolicy.label_ids.length === 0 && filterPolicy.priorities.length === 0)
      return;

    const wasUpdated = await updatePolicy({ ...workspaceStatus.policy, ...filterPolicy }, "filter");
    if (wasUpdated) setFilterPolicyDraft(null);
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
  const filterPolicy = getAvailableFilterPolicy(
    filterPolicyDraft ?? getFilterPolicy(workspaceStatus.policy),
    filterOptions
  );
  const isFilterSelectionEmpty = filterPolicy.label_ids.length === 0 && filterPolicy.priorities.length === 0;
  const hasFilterPolicyChanged =
    filterPolicy.mode !== workspaceStatus.policy.mode ||
    filterPolicy.label_match !== workspaceStatus.policy.label_match ||
    filterPolicy.label_ids.join(",") !== workspaceStatus.policy.label_ids.join(",") ||
    filterPolicy.priorities.join(",") !== workspaceStatus.policy.priorities.join(",");
  const priorityOptions = ISSUE_PRIORITIES.filter((priority) =>
    filterOptions?.priorities.some((option) => option.key === priority.key)
  ).map((priority) => ({
    value: priority.title,
    data: priority,
  }));
  const labelOptions = filterOptions?.labels.map((label) => ({
    value: label.name,
    data: label,
  }));
  const selectedPriorityNames = priorityOptions
    .filter((option) => filterPolicy.priorities.includes(option.data.key))
    .map((option) => option.value)
    .join(", ");
  const selectedLabelNames =
    labelOptions
      ?.filter((option) => filterPolicy.label_ids.includes(option.data.id))
      .map((option) => option.value)
      .join(", ") ?? "";

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
          <SettingsBoxedControlItem
            title={t("workspace_settings.settings.integrations.google_calendar.workspace_policy.mode.title")}
            description={t(
              "workspace_settings.settings.integrations.google_calendar.workspace_policy.mode.description"
            )}
            control={
              <CustomSelect
                value={filterPolicy.mode}
                onChange={(mode: TFilterPolicyDraft["mode"]) => setFilterPolicyDraft({ ...filterPolicy, mode })}
                label={t(
                  `workspace_settings.settings.integrations.google_calendar.workspace_policy.mode.options.${filterPolicy.mode}`
                )}
                disabled={isMutating}
                buttonClassName="min-w-44 border border-subtle-1"
                input
                placement="bottom-end"
              >
                <CustomSelect.Option value="assignment">
                  {t(
                    "workspace_settings.settings.integrations.google_calendar.workspace_policy.mode.options.assignment"
                  )}
                </CustomSelect.Option>
                <CustomSelect.Option value="filter">
                  {t("workspace_settings.settings.integrations.google_calendar.workspace_policy.mode.options.filter")}
                </CustomSelect.Option>
              </CustomSelect>
            }
          />
          {filterPolicy.mode === "filter" && (
            <>
              <SettingsBoxedControlItem
                title={t("workspace_settings.settings.integrations.google_calendar.workspace_policy.priorities.title")}
                description={t(
                  "workspace_settings.settings.integrations.google_calendar.workspace_policy.priorities.description"
                )}
                control={
                  <MultiSelectDropdown
                    value={filterPolicy.priorities}
                    onChange={(priorities) =>
                      setFilterPolicyDraft({ ...filterPolicy, priorities: priorities.filter(isIssuePriority) })
                    }
                    options={filterOptions ? priorityOptions : undefined}
                    keyExtractor={(option) => option.data.key}
                    queryArray={["title"]}
                    buttonContent={() => (
                      <span className="block max-w-64 truncate rounded border border-subtle-1 px-3 py-2 text-13 text-primary">
                        {selectedPriorityNames ||
                          t(
                            "workspace_settings.settings.integrations.google_calendar.workspace_policy.priorities.placeholder"
                          )}
                      </span>
                    )}
                    disabled={isMutating || Boolean(filterOptionsError)}
                    placement="bottom-end"
                    disableSearch
                    disableSorting
                  />
                }
              />
              <SettingsBoxedControlItem
                title={t("workspace_settings.settings.integrations.google_calendar.workspace_policy.labels.title")}
                description={t(
                  "workspace_settings.settings.integrations.google_calendar.workspace_policy.labels.description"
                )}
                control={
                  <MultiSelectDropdown
                    value={filterPolicy.label_ids}
                    onChange={(labelIds) => setFilterPolicyDraft({ ...filterPolicy, label_ids: labelIds })}
                    options={labelOptions}
                    keyExtractor={(option) => option.data.id}
                    queryArray={["name"]}
                    buttonContent={() => (
                      <span className="block max-w-64 truncate rounded border border-subtle-1 px-3 py-2 text-13 text-primary">
                        {selectedLabelNames ||
                          t(
                            "workspace_settings.settings.integrations.google_calendar.workspace_policy.labels.placeholder"
                          )}
                      </span>
                    )}
                    renderItem={({ value }) => {
                      const label = filterOptions?.labels.find((option) => option.id === value);
                      if (!label) return null;

                      return (
                        <span className="flex min-w-0 items-center gap-2">
                          <span
                            className="h-2.5 w-2.5 shrink-0 rounded-full"
                            style={{ backgroundColor: label.color }}
                          />
                          <span className="truncate">{label.name}</span>
                        </span>
                      );
                    }}
                    disabled={isMutating || Boolean(filterOptionsError)}
                    placement="bottom-end"
                    disableSorting
                  />
                }
              />
              <SettingsBoxedControlItem
                title={t("workspace_settings.settings.integrations.google_calendar.workspace_policy.label_match.title")}
                description={t(
                  "workspace_settings.settings.integrations.google_calendar.workspace_policy.label_match.description"
                )}
                control={
                  <CustomSelect
                    value={filterPolicy.label_match}
                    onChange={(labelMatch: TFilterPolicyDraft["label_match"]) =>
                      setFilterPolicyDraft({ ...filterPolicy, label_match: labelMatch })
                    }
                    label={t(
                      `workspace_settings.settings.integrations.google_calendar.workspace_policy.label_match.options.${filterPolicy.label_match}`
                    )}
                    disabled={isMutating}
                    buttonClassName="min-w-44 border border-subtle-1"
                    input
                    placement="bottom-end"
                  >
                    <CustomSelect.Option value="any">
                      {t(
                        "workspace_settings.settings.integrations.google_calendar.workspace_policy.label_match.options.any"
                      )}
                    </CustomSelect.Option>
                    <CustomSelect.Option value="all">
                      {t(
                        "workspace_settings.settings.integrations.google_calendar.workspace_policy.label_match.options.all"
                      )}
                    </CustomSelect.Option>
                  </CustomSelect>
                }
              />
              <p className="text-caption-md-regular text-tertiary">
                {t("workspace_settings.settings.integrations.google_calendar.workspace_policy.filter_composition")}
              </p>
            </>
          )}
          {filterPolicy.mode === "filter" && filterOptionsError && (
            <div className="flex items-center justify-between gap-4 rounded-lg border border-danger-subtle bg-danger-subtle px-4 py-3">
              <p className="text-caption-md-regular text-danger-primary">
                {t("workspace_settings.settings.integrations.google_calendar.workspace_policy.filter_options_error")}
              </p>
              <Button variant="secondary" onClick={() => void mutateFilterOptions()}>
                {t("workspace_settings.settings.integrations.google_calendar.workspace_policy.retry")}
              </Button>
            </div>
          )}
          {filterPolicy.mode === "filter" && isFilterSelectionEmpty && (
            <p className="text-caption-md-regular text-danger-primary">
              {t("workspace_settings.settings.integrations.google_calendar.workspace_policy.filter_validation")}
            </p>
          )}
          <div>
            <Button
              variant="primary"
              onClick={() => void handleFilterSave()}
              loading={policyMutation === "filter"}
              disabled={
                isMutating ||
                !hasFilterPolicyChanged ||
                (filterPolicy.mode === "filter" && (isFilterSelectionEmpty || !filterOptions))
              }
            >
              {t("workspace_settings.settings.integrations.google_calendar.workspace_policy.save_filter")}
            </Button>
          </div>
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
