/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { API_BASE_URL } from "@plane/constants";
import type {
  IAppIntegration,
  IExportServiceResponse,
  IGoogleCalendarConnectionRosterItem,
  IGoogleCalendarDisconnectResponse,
  IGoogleCalendarFilterOptions,
  IGoogleCalendarWorkspacePolicy,
  IGoogleCalendarWorkspaceStatus,
  IImporterService,
  IWorkspaceIntegration,
} from "@plane/types";
import { APIService } from "@/services/api.service";
// types
// helper

export class IntegrationService extends APIService {
  constructor() {
    super(API_BASE_URL);
  }

  async getAppIntegrationsList(): Promise<IAppIntegration[]> {
    return this.get(`/api/integrations/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getWorkspaceIntegrationsList(workspaceSlug: string): Promise<IWorkspaceIntegration[]> {
    return this.get(`/api/workspaces/${workspaceSlug}/workspace-integrations/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async deleteWorkspaceIntegration(workspaceSlug: string, integrationId: string): Promise<any> {
    return this.delete(`/api/workspaces/${workspaceSlug}/workspace-integrations/${integrationId}/provider/`)
      .then((res) => res?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getGoogleCalendarStatus(workspaceSlug: string): Promise<IGoogleCalendarWorkspaceStatus> {
    return this.get(`/api/workspaces/${workspaceSlug}/integrations/google-calendar/status/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async updateGoogleCalendarPolicy(
    workspaceSlug: string,
    policy: IGoogleCalendarWorkspacePolicy
  ): Promise<IGoogleCalendarWorkspacePolicy> {
    return this.patch(`/api/workspaces/${workspaceSlug}/integrations/google-calendar/policy/`, policy)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getGoogleCalendarFilterOptions(workspaceSlug: string): Promise<IGoogleCalendarFilterOptions> {
    return this.get(`/api/workspaces/${workspaceSlug}/integrations/google-calendar/filter-options/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getGoogleCalendarConnectionRoster(workspaceSlug: string): Promise<IGoogleCalendarConnectionRosterItem[]> {
    return this.get(`/api/workspaces/${workspaceSlug}/integrations/google-calendar/connections/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async disconnectGoogleCalendarConnection(
    workspaceSlug: string,
    memberId: string
  ): Promise<IGoogleCalendarDisconnectResponse> {
    return this.delete(`/api/workspaces/${workspaceSlug}/integrations/google-calendar/connections/${memberId}/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async getImporterServicesList(workspaceSlug: string): Promise<IImporterService[]> {
    return this.get(`/api/workspaces/${workspaceSlug}/importers/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
  async getExportsServicesList(
    workspaceSlug: string,
    cursor: string,
    per_page: number
  ): Promise<IExportServiceResponse> {
    return this.get(`/api/workspaces/${workspaceSlug}/export-issues`, {
      params: {
        per_page,
        cursor,
      },
    })
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }

  async deleteImporterService(workspaceSlug: string, service: string, importerId: string): Promise<any> {
    return this.delete(`/api/workspaces/${workspaceSlug}/importers/${service}/${importerId}/`)
      .then((response) => response?.data)
      .catch((error) => {
        throw error?.response?.data;
      });
  }
}
