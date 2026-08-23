/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { CalendarDays, ChevronRight } from "lucide-react";
import { observer } from "mobx-react";
import Link from "next/link";
// plane imports
import { useTranslation } from "@plane/i18n";
import { Card, ECardDirection, ECardSpacing } from "@plane/propel/card";
// components
import { NotAuthorizedView } from "@/components/auth-screens/not-authorized-view";
import { PageHead } from "@/components/core/page-title";
import { SettingsContentWrapper } from "@/components/settings/content-wrapper";
import { SettingsHeading } from "@/components/settings/heading";
// hooks
import { useInstance } from "@/hooks/store/use-instance";
import { useWorkspace } from "@/hooks/store/use-workspace";
// local imports
import type { Route } from "./+types/page";
import { IntegrationsWorkspaceSettingsHeader } from "./header";

function WorkspaceIntegrationsPage({ params }: Route.ComponentProps) {
  // router
  const { workspaceSlug } = params;
  // store hooks
  const { config } = useInstance();
  const { currentWorkspace } = useWorkspace();
  // translation
  const { t } = useTranslation();
  // derived values
  const isGoogleCalendarAvailable = config?.is_google_calendar_available ?? false;
  const pageTitle = currentWorkspace?.name
    ? `${currentWorkspace.name} - ${t("workspace_settings.settings.integrations.title")}`
    : undefined;

  if (!isGoogleCalendarAvailable) return <NotAuthorizedView section="settings" className="h-auto" />;

  return (
    <SettingsContentWrapper header={<IntegrationsWorkspaceSettingsHeader />}>
      <PageHead title={pageTitle} />
      <div className="flex w-full flex-col gap-y-6">
        <SettingsHeading
          title={t("workspace_settings.settings.integrations.heading")}
          description={t("workspace_settings.settings.integrations.description")}
        />
        <Link href={`/${workspaceSlug}/settings/integrations/google-calendar/`}>
          <Card direction={ECardDirection.ROW} spacing={ECardSpacing.SM} className="items-center">
            <div className="flex size-10 shrink-0 items-center justify-center rounded-md bg-layer-2">
              <CalendarDays className="size-5 text-primary" />
            </div>
            <div className="min-w-0 flex-1">
              <h4 className="text-body-sm-medium text-primary">
                {t("workspace_settings.settings.integrations.google_calendar.title")}
              </h4>
              <p className="text-body-xs-regular text-tertiary">
                {t("workspace_settings.settings.integrations.google_calendar.card_description")}
              </p>
            </div>
            <ChevronRight className="size-4 shrink-0 text-tertiary" />
          </Card>
        </Link>
      </div>
    </SettingsContentWrapper>
  );
}

export default observer(WorkspaceIntegrationsPage);
