/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { AlertCircle, CheckCircle2, CircleDashed, Unplug } from "lucide-react";
// plane imports
import { useTranslation } from "@plane/i18n";
import type { TGoogleCalendarConnectionStatus } from "@plane/types";
import { cn } from "@plane/utils";

const STATUS_DETAILS: Record<
  TGoogleCalendarConnectionStatus,
  { icon: typeof CheckCircle2; iconClassName: string; labelKey: string }
> = {
  provisioning: {
    icon: CircleDashed,
    iconClassName: "animate-spin text-accent-primary",
    labelKey: "workspace_settings.settings.integrations.google_calendar.member_connection.status.provisioning",
  },
  healthy: {
    icon: CheckCircle2,
    iconClassName: "text-success-primary",
    labelKey: "workspace_settings.settings.integrations.google_calendar.member_connection.status.healthy",
  },
  broken: {
    icon: AlertCircle,
    iconClassName: "text-danger-primary",
    labelKey: "workspace_settings.settings.integrations.google_calendar.member_connection.status.broken",
  },
  disconnecting: {
    icon: CircleDashed,
    iconClassName: "animate-spin text-secondary",
    labelKey: "workspace_settings.settings.integrations.google_calendar.member_connection.status.disconnecting",
  },
};

type Props = {
  status: TGoogleCalendarConnectionStatus | null;
};

export const GoogleCalendarConnectionStatus = ({ status }: Props) => {
  const { t } = useTranslation();

  if (!status)
    return (
      <div className="flex items-center gap-2 text-body-sm-medium text-secondary">
        <Unplug className="size-4" />
        {t("workspace_settings.settings.integrations.google_calendar.member_connection.status.disconnected")}
      </div>
    );

  const statusDetails = STATUS_DETAILS[status];
  const Icon = statusDetails.icon;

  return (
    <div className="flex items-center gap-2 text-body-sm-medium text-primary">
      <Icon className={cn("size-4", statusDetails.iconClassName)} />
      {t(statusDetails.labelKey)}
    </div>
  );
};
