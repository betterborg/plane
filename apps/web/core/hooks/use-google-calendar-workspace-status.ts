/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import useSWR from "swr";
// plane imports
import { GOOGLE_CALENDAR_STATUS } from "@plane/constants";
import type { IGoogleCalendarWorkspaceStatus } from "@plane/types";
// services
import { IntegrationService } from "@/services/integrations";

const integrationService = new IntegrationService();

export const useGoogleCalendarWorkspaceStatus = (workspaceSlug: string | undefined, shouldFetch = true) =>
  useSWR<IGoogleCalendarWorkspaceStatus>(
    workspaceSlug && shouldFetch ? GOOGLE_CALENDAR_STATUS(workspaceSlug) : null,
    () => integrationService.getGoogleCalendarStatus(workspaceSlug ?? "")
  );
