/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
// plane imports
import { useTranslation } from "@plane/i18n";
// components
import { NotAuthorizedView } from "@/components/auth-screens/not-authorized-view";
import { PageHead } from "@/components/core/page-title";
import { SettingsContentWrapper } from "@/components/settings/content-wrapper";
import { SettingsHeading } from "@/components/settings/heading";
// hooks
import { useInstance } from "@/hooks/store/use-instance";
import { useWorkspace } from "@/hooks/store/use-workspace";
// local imports
import { GoogleCalendarWorkspaceSettingsHeader } from "./header";

function GoogleCalendarSettingsPage() {
  // store hooks
  const { config } = useInstance();
  const { currentWorkspace } = useWorkspace();
  // translation
  const { t } = useTranslation();
  // derived values
  const isGoogleCalendarAvailable = config?.is_google_calendar_available ?? false;
  const pageTitle = currentWorkspace?.name
    ? `${currentWorkspace.name} - ${t("workspace_settings.settings.integrations.google_calendar.title")}`
    : undefined;

  if (!isGoogleCalendarAvailable) return <NotAuthorizedView section="settings" className="h-auto" />;

  return (
    <SettingsContentWrapper header={<GoogleCalendarWorkspaceSettingsHeader />}>
      <PageHead title={pageTitle} />
      <SettingsHeading
        title={t("workspace_settings.settings.integrations.google_calendar.heading")}
        description={t("workspace_settings.settings.integrations.google_calendar.description")}
      />
    </SettingsContentWrapper>
  );
}

export default observer(GoogleCalendarSettingsPage);
