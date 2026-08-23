/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { CalendarDays } from "lucide-react";
import { observer } from "mobx-react";
import { useParams } from "react-router";
// plane imports
import { WORKSPACE_SETTINGS } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { Breadcrumbs } from "@plane/ui";
// components
import { BreadcrumbLink } from "@/components/common/breadcrumb-link";
import { SettingsPageHeader } from "@/components/settings/page-header";
import { WORKSPACE_SETTINGS_ICONS } from "@/components/settings/workspace/sidebar/item-icon";

export const GoogleCalendarWorkspaceSettingsHeader = observer(function GoogleCalendarWorkspaceSettingsHeader() {
  // router
  const { workspaceSlug } = useParams();
  // translation
  const { t } = useTranslation();
  // derived values
  const settingsDetails = WORKSPACE_SETTINGS.integrations;
  const Icon = WORKSPACE_SETTINGS_ICONS.integrations;

  return (
    <SettingsPageHeader
      leftItem={
        <div className="flex items-center gap-2">
          <Breadcrumbs>
            <Breadcrumbs.Item
              component={
                <BreadcrumbLink
                  href={`/${workspaceSlug}/settings/integrations/`}
                  label={t(settingsDetails.i18n_label)}
                  icon={<Icon className="size-4 text-tertiary" />}
                />
              }
            />
            <Breadcrumbs.Item
              component={
                <BreadcrumbLink
                  label={t("workspace_settings.settings.integrations.google_calendar.title")}
                  icon={<CalendarDays className="size-4 text-tertiary" />}
                  isLast
                />
              }
            />
          </Breadcrumbs>
        </div>
      }
    />
  );
});
