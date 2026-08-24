/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
// plane imports
import { EUserPermissionsLevel, PROJECT_SETTINGS } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
// components
import type { TPowerKContext } from "@/components/power-k/core/types";
import { PowerKSettingsMenu } from "@/components/power-k/menus/settings";
import { PROJECT_SETTINGS_ICONS } from "@/components/settings/project/sidebar/item-icon";
// hooks
import { useGoogleCalendarWorkspaceStatus } from "@/hooks/use-google-calendar-workspace-status";
import { useInstance } from "@/hooks/store/use-instance";
import { useUserPermissions } from "@/hooks/store/user";

type Props = {
  context: TPowerKContext;
  handleSelect: (href: string) => void;
};

export const PowerKOpenProjectSettingsMenu = observer(function PowerKOpenProjectSettingsMenu(props: Props) {
  const { context, handleSelect } = props;
  // plane hooks
  const { t } = useTranslation();
  // store hooks
  const { config } = useInstance();
  const { allowPermissions } = useUserPermissions();
  // derived values
  const workspaceSlug = context.params.workspaceSlug?.toString();
  const projectId = context.params.projectId?.toString();
  const canAccessGoogleCalendar = Boolean(
    workspaceSlug &&
    projectId &&
    allowPermissions(
      PROJECT_SETTINGS.features_google_calendar.access,
      EUserPermissionsLevel.PROJECT,
      workspaceSlug,
      projectId
    )
  );
  const { data: googleCalendarStatus } = useGoogleCalendarWorkspaceStatus(
    workspaceSlug,
    Boolean(config?.is_google_calendar_available && canAccessGoogleCalendar)
  );
  const settingsList = Object.values(PROJECT_SETTINGS).filter(
    (setting) =>
      workspaceSlug &&
      projectId &&
      (setting.key !== "features_google_calendar" || googleCalendarStatus?.policy.enabled) &&
      allowPermissions(setting.access, EUserPermissionsLevel.PROJECT, workspaceSlug, projectId)
  );
  const settingsListWithIcons = settingsList.map((setting) => ({
    key: setting.key,
    i18n_label: setting.i18n_label,
    href: setting.href,
    access: setting.access,
    highlight: setting.highlight,
    label: t(setting.i18n_label),
    icon: PROJECT_SETTINGS_ICONS[setting.key],
  }));

  return <PowerKSettingsMenu settings={settingsListWithIcons} onSelect={(setting) => handleSelect(setting.href)} />;
});
