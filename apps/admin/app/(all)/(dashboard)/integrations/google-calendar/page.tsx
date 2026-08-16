/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
import useSWR from "swr";
import { Loader } from "@plane/ui";
// components
import { PageWrapper } from "@/components/common/page-wrapper";
// hooks
import { useInstance } from "@/hooks/store";
// types
import type { Route } from "./+types/page";
// local imports
import { InstanceGoogleCalendarConfigForm } from "./form";

const InstanceGoogleCalendarPage = observer(function InstanceGoogleCalendarPage(_props: Route.ComponentProps) {
  const { fetchInstanceConfigurations, formattedConfig } = useInstance();

  useSWR("INSTANCE_CONFIGURATIONS", () => fetchInstanceConfigurations());

  return (
    <PageWrapper
      header={{
        title: "Google Calendar integration",
        description: "Configure the isolated Google OAuth client used by Calendar for this Plane instance.",
      }}
      size="lg"
    >
      {formattedConfig ? (
        <InstanceGoogleCalendarConfigForm config={formattedConfig} />
      ) : (
        <Loader className="space-y-8">
          <Loader.Item height="50px" width="40%" />
          <div className="grid grid-cols-1 gap-8 lg:grid-cols-2">
            <Loader.Item height="240px" />
            <Loader.Item height="240px" />
          </div>
        </Loader>
      )}
    </PageWrapper>
  );
});

export const meta: Route.MetaFunction = () => [{ title: "Google Calendar Integration - God Mode" }];

export default InstanceGoogleCalendarPage;
