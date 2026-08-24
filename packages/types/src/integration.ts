/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { TIssuePriorities } from "./issues";
import type { IUserLite } from "./users";

// All the app integrations that are available
export interface IAppIntegration {
  author: string;
  avatar_url: string | null;
  created_at: string;
  created_by: string | null;
  description: any;
  id: string;
  metadata: any;
  network: number;
  provider: string;
  redirect_url: string;
  title: string;
  updated_at: string;
  updated_by: string | null;
  verified: boolean;
  webhook_secret: string;
  webhook_url: string;
}

export interface IWorkspaceIntegration {
  actor: string;
  api_token: string;
  config: any;
  created_at: string;
  created_by: string;
  id: string;
  integration: string;
  integration_detail: IAppIntegration;
  metadata: any;
  updated_at: string;
  updated_by: string;
  workspace: string;
}

export type TGoogleCalendarConnectionStatus = "provisioning" | "healthy" | "broken" | "disconnecting";

export interface IGoogleCalendarWorkspacePolicy {
  enabled: boolean;
  mode: "assignment" | "filter";
  update_on_completion: boolean;
  recipients: "cycle_members";
  label_ids: string[];
  priorities: TIssuePriorities[];
  label_match: "any" | "all";
}

export interface IGoogleCalendarConnectionStatus {
  status: TGoogleCalendarConnectionStatus;
}

export interface IGoogleCalendarWorkspaceStatus {
  available: boolean;
  policy: IGoogleCalendarWorkspacePolicy;
  connection: IGoogleCalendarConnectionStatus | null;
}

export interface IGoogleCalendarFilterOptionLabel {
  id: string;
  name: string;
  color: string;
  project_id: string | null;
}

export interface IGoogleCalendarFilterOptions {
  labels: IGoogleCalendarFilterOptionLabel[];
  priorities: {
    key: TIssuePriorities;
    title: string;
  }[];
}

export interface IGoogleCalendarProjectSync {
  google_calendar_sync_enabled: boolean;
}

export interface IGoogleCalendarConnectionRosterItem {
  member: IUserLite;
  connection: IGoogleCalendarConnectionStatus & {
    last_success_at: string | null;
  };
}

export interface IGoogleCalendarDisconnectResponse {
  status: "disconnecting";
}

// slack integration
export interface ISlackIntegration {
  id: string;
  created_at: string;
  updated_at: string;
  access_token: string;
  scopes: string;
  bot_user_id: string;
  webhook_url: string;
  data: ISlackIntegrationData;
  team_id: string;
  team_name: string;
  created_by: string;
  updated_by: string;
  project: string;
  workspace: string;
  workspace_integration: string;
}

export interface ISlackIntegrationData {
  ok: boolean;
  team: {
    id: string;
    name: string;
  };
  scope: string;
  app_id: string;
  enterprise: any;
  token_type: string;
  authed_user: string;
  bot_user_id: string;
  access_token: string;
  incoming_webhook: {
    url: string;
    channel: string;
    channel_id: string;
    configuration_url: string;
  };
  is_enterprise_install: boolean;
}
