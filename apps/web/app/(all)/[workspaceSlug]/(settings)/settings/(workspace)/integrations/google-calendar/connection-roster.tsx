/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { AlertCircle } from "lucide-react";
import { observer } from "mobx-react";
import useSWR from "swr";
// plane imports
import { GOOGLE_CALENDAR_CONNECTION_ROSTER } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { Banner } from "@plane/propel/banner";
import { Button } from "@plane/propel/button";
import type { IGoogleCalendarConnectionRosterItem } from "@plane/types";
import { EUserWorkspaceRoles } from "@plane/types";
import { Avatar, Loader } from "@plane/ui";
import { getFileURL } from "@plane/utils";
// hooks
import { useUserPermissions } from "@/hooks/store/user";
// services
import { IntegrationService } from "@/services/integrations";
// local imports
import { GoogleCalendarConnectionStatus } from "./connection-status";

type Props = {
  workspaceSlug: string;
};

const integrationService = new IntegrationService();

export const GoogleCalendarConnectionRoster = observer(function GoogleCalendarConnectionRoster({
  workspaceSlug,
}: Props) {
  // store hooks
  const { getWorkspaceRoleByWorkspaceSlug } = useUserPermissions();
  // translation
  const { t } = useTranslation();
  // derived values
  const isAdmin = getWorkspaceRoleByWorkspaceSlug(workspaceSlug) === EUserWorkspaceRoles.ADMIN;

  const {
    data: connectionRoster,
    error: connectionRosterError,
    isLoading,
    mutate: mutateConnectionRoster,
  } = useSWR<IGoogleCalendarConnectionRosterItem[]>(
    isAdmin ? GOOGLE_CALENDAR_CONNECTION_ROSTER(workspaceSlug) : null,
    () => integrationService.getGoogleCalendarConnectionRoster(workspaceSlug)
  );

  if (!isAdmin) return null;

  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h3 className="text-body-md-semibold text-primary">
          {t("workspace_settings.settings.integrations.google_calendar.connection_roster.title")}
        </h3>
        <p className="text-body-sm-regular text-secondary">
          {t("workspace_settings.settings.integrations.google_calendar.connection_roster.description")}
        </p>
      </div>

      {isLoading && !connectionRoster ? (
        <Loader className="space-y-3 rounded-lg border border-subtle p-4">
          <Loader.Item height="24px" width="100%" />
          <Loader.Item height="32px" width="100%" />
          <Loader.Item height="32px" width="100%" />
        </Loader>
      ) : connectionRosterError ? (
        <Banner
          variant="error"
          icon={<AlertCircle className="size-4" />}
          title={t("workspace_settings.settings.integrations.google_calendar.connection_roster.load_error")}
          action={
            <Button variant="secondary" onClick={() => void mutateConnectionRoster()}>
              {t("workspace_settings.settings.integrations.google_calendar.connection_roster.retry")}
            </Button>
          }
        />
      ) : connectionRoster && connectionRoster.length > 0 ? (
        <div className="overflow-hidden rounded-lg border border-subtle">
          <table className="w-full table-fixed">
            <thead className="border-b border-subtle bg-layer-2 text-left text-caption-md-medium text-secondary">
              <tr>
                <th className="w-1/2 px-4 py-2 font-medium" scope="col">
                  {t("workspace_settings.settings.integrations.google_calendar.connection_roster.member")}
                </th>
                <th className="w-1/2 px-4 py-2 font-medium" scope="col">
                  {t("workspace_settings.settings.integrations.google_calendar.connection_roster.status")}
                </th>
              </tr>
            </thead>
            <tbody>
              {connectionRoster.map(({ member, connection }) => (
                <tr key={member.id} className="border-b border-subtle last:border-b-0">
                  <td className="px-4 py-3">
                    <div className="flex min-w-0 items-center gap-3">
                      <Avatar name={member.display_name} src={getFileURL(member.avatar_url)} size="sm" />
                      <span className="truncate text-body-sm-medium text-primary">{member.display_name}</span>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <GoogleCalendarConnectionStatus status={connection.status} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="rounded-lg border border-subtle bg-layer-2 px-4 py-6 text-center text-body-sm-regular text-secondary">
          {t("workspace_settings.settings.integrations.google_calendar.connection_roster.empty")}
        </div>
      )}
    </section>
  );
});
