/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
import useSWR from "swr";
// plane imports
import { EUserPermissions, GOOGLE_CALENDAR_PROJECT_SYNC } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { setPromiseToast } from "@plane/propel/toast";
import type { IGoogleCalendarProjectSync } from "@plane/types";
import { Loader, ToggleSwitch } from "@plane/ui";
// components
import { NotAuthorizedView } from "@/components/auth-screens/not-authorized-view";
import { PageHead } from "@/components/core/page-title";
import { SettingsBoxedControlItem } from "@/components/settings/boxed-control-item";
import { SettingsContentWrapper } from "@/components/settings/content-wrapper";
import { SettingsHeading } from "@/components/settings/heading";
// hooks
import { useGoogleCalendarWorkspaceStatus } from "@/hooks/use-google-calendar-workspace-status";
import { useInstance } from "@/hooks/store/use-instance";
import { useProject } from "@/hooks/store/use-project";
import { useUserPermissions } from "@/hooks/store/user";
// services
import { IntegrationService } from "@/services/integrations";
// local imports
import type { Route } from "./+types/page";
import { FeaturesGoogleCalendarProjectSettingsHeader } from "./header";

const integrationService = new IntegrationService();

function FeaturesGoogleCalendarSettingsPage({ params }: Route.ComponentProps) {
  const { workspaceSlug, projectId } = params;
  // states
  const [isUpdating, setIsUpdating] = useState(false);
  // store hooks
  const { config } = useInstance();
  const { currentProjectDetails } = useProject();
  const { getProjectMembershipRoleByWorkspaceSlugAndProjectId } = useUserPermissions();
  // translation
  const { t } = useTranslation();
  // derived values
  const isGoogleCalendarAvailable = config?.is_google_calendar_available ?? false;
  const isProjectAdmin =
    getProjectMembershipRoleByWorkspaceSlugAndProjectId(workspaceSlug, projectId) === EUserPermissions.ADMIN;
  const pageTitle = currentProjectDetails?.name
    ? `${currentProjectDetails.name} settings - ${t("project_settings.features.google_calendar.short_title")}`
    : undefined;
  // data fetching
  const { data: workspaceStatus, error: workspaceStatusError } = useGoogleCalendarWorkspaceStatus(
    workspaceSlug,
    isGoogleCalendarAvailable && isProjectAdmin
  );
  const {
    data: projectSync,
    error: projectSyncError,
    mutate: mutateProjectSync,
  } = useSWR<IGoogleCalendarProjectSync>(
    workspaceStatus?.policy.enabled && isProjectAdmin ? GOOGLE_CALENDAR_PROJECT_SYNC(workspaceSlug, projectId) : null,
    () => integrationService.getGoogleCalendarProjectSync(workspaceSlug, projectId)
  );

  const handleSyncChange = async (enabled: boolean) => {
    if (!projectSync || enabled === projectSync.google_calendar_sync_enabled) return;

    setIsUpdating(true);
    const updatePromise = integrationService.updateGoogleCalendarProjectSync(workspaceSlug, projectId, {
      google_calendar_sync_enabled: enabled,
    });
    setPromiseToast(updatePromise, {
      loading: t("project_settings.features.google_calendar.toast.loading"),
      success: {
        title: t("project_settings.features.google_calendar.toast.success.title"),
        message: () => t("project_settings.features.google_calendar.toast.success.message"),
      },
      error: {
        title: t("project_settings.features.google_calendar.toast.error.title"),
        message: () => t("project_settings.features.google_calendar.toast.error.message"),
      },
    });

    try {
      const updatedProjectSync = await updatePromise;
      await mutateProjectSync(updatedProjectSync, { revalidate: false });
    } catch {
      // The promise toast reports the mutation failure to the user.
    } finally {
      setIsUpdating(false);
    }
  };

  if (
    !isGoogleCalendarAvailable ||
    !isProjectAdmin ||
    workspaceStatusError ||
    (workspaceStatus && !workspaceStatus.policy.enabled)
  ) {
    return <NotAuthorizedView section="settings" isProjectView className="h-auto" />;
  }

  return (
    <SettingsContentWrapper header={<FeaturesGoogleCalendarProjectSettingsHeader />}>
      <PageHead title={pageTitle} />
      <section className="w-full">
        <SettingsHeading
          title={t("project_settings.features.google_calendar.title")}
          description={t("project_settings.features.google_calendar.description")}
        />
        <div className="mt-7">
          {projectSync ? (
            <SettingsBoxedControlItem
              title={t("project_settings.features.google_calendar.toggle_title")}
              description={t("project_settings.features.google_calendar.toggle_description")}
              control={
                <ToggleSwitch
                  value={projectSync.google_calendar_sync_enabled}
                  onChange={(enabled) => void handleSyncChange(enabled)}
                  disabled={isUpdating}
                  size="sm"
                />
              }
            />
          ) : projectSyncError ? (
            <div className="rounded-lg border border-subtle p-4 text-body-sm-regular text-secondary">
              {t("project_settings.features.google_calendar.load_error")}
            </div>
          ) : (
            <Loader className="rounded-lg border border-subtle p-4">
              <Loader.Item height="48px" width="100%" />
            </Loader>
          )}
        </div>
      </section>
    </SettingsContentWrapper>
  );
}

export default observer(FeaturesGoogleCalendarSettingsPage);
