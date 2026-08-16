/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { Controller, useForm } from "react-hook-form";
import { CalendarCheck, CircleAlert } from "lucide-react";
// plane internal packages
import { API_BASE_URL } from "@plane/constants";
import { Button } from "@plane/propel/button";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import type { IFormattedInstanceConfiguration, TInstanceGoogleCalendarConfigurationKeys } from "@plane/types";
import { Checkbox } from "@plane/ui";
// components
import { CodeBlock } from "@/components/common/code-block";
import type { TControllerInputFormField } from "@/components/common/controller-input";
import { ControllerInput } from "@/components/common/controller-input";
import { CopyField } from "@/components/common/copy-field";
// hooks
import { useInstance } from "@/hooks/store";

type Props = {
  config: IFormattedInstanceConfiguration;
};

type GoogleCalendarConfigFormValues = Record<TInstanceGoogleCalendarConfigurationKeys, string>;

type ConfigurationError = {
  error?: string;
};

const isCalendarCredentialsLockedError = (error: unknown): error is ConfigurationError =>
  typeof error === "object" &&
  error !== null &&
  "error" in error &&
  (error as ConfigurationError).error === "google_calendar_credentials_locked";

export function InstanceGoogleCalendarConfigForm(props: Props) {
  const { config } = props;
  const { updateInstanceConfigurations } = useInstance();
  const {
    control,
    handleSubmit,
    reset,
    formState: { errors, isDirty, isSubmitting },
  } = useForm<GoogleCalendarConfigFormValues>({
    defaultValues: {
      GOOGLE_CALENDAR_CLIENT_ID: config.GOOGLE_CALENDAR_CLIENT_ID ?? "",
      GOOGLE_CALENDAR_CLIENT_SECRET: config.GOOGLE_CALENDAR_CLIENT_SECRET ?? "",
      GOOGLE_CALENDAR_IS_PROJECT_DEDICATED: config.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED ?? "0",
    },
  });

  const configuredOrigin = API_BASE_URL || (typeof window !== "undefined" ? window.location.origin : "");
  const callbackUri = `${configuredOrigin.replace(/\/$/, "")}/auth/google-calendar/callback/`;

  const formFields: TControllerInputFormField[] = [
    {
      key: "GOOGLE_CALENDAR_CLIENT_ID",
      type: "text",
      label: "OAuth client ID",
      description: "The client ID from the dedicated Google Cloud project's Web application OAuth client.",
      placeholder: "840195096245-example.apps.googleusercontent.com",
      error: Boolean(errors.GOOGLE_CALENDAR_CLIENT_ID),
      required: true,
    },
    {
      key: "GOOGLE_CALENDAR_CLIENT_SECRET",
      type: "password",
      label: "OAuth client secret",
      description: "The client secret is sent to Plane's encrypted instance-configuration store.",
      placeholder: "GOCSPX-example",
      error: Boolean(errors.GOOGLE_CALENDAR_CLIENT_SECRET),
      required: true,
    },
  ];

  const onSubmit = async (formData: GoogleCalendarConfigFormValues) => {
    try {
      const response = await updateInstanceConfigurations(formData);
      const savedValues = Object.fromEntries(response.map(({ key, value }) => [key, value]));

      reset({
        GOOGLE_CALENDAR_CLIENT_ID: savedValues.GOOGLE_CALENDAR_CLIENT_ID ?? formData.GOOGLE_CALENDAR_CLIENT_ID,
        GOOGLE_CALENDAR_CLIENT_SECRET:
          savedValues.GOOGLE_CALENDAR_CLIENT_SECRET ?? formData.GOOGLE_CALENDAR_CLIENT_SECRET,
        GOOGLE_CALENDAR_IS_PROJECT_DEDICATED:
          savedValues.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED ?? formData.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED,
      });
      setToast({
        type: TOAST_TYPE.SUCCESS,
        title: "Configuration saved",
        message: "Google Calendar OAuth credentials were saved successfully.",
      });
    } catch (error) {
      if (isCalendarCredentialsLockedError(error)) {
        setToast({
          type: TOAST_TYPE.ERROR,
          title: "Calendar credentials are locked",
          message:
            "Live credential rotation is not supported. Revoke Calendar access project-wide before replacing this OAuth client.",
        });
        return;
      }

      console.error(error);
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Could not save configuration",
        message: "Check the credentials and try again.",
      });
    }
  };

  return (
    <form className="space-y-8" onSubmit={handleSubmit(onSubmit)}>
      <div className="grid grid-cols-1 gap-8 lg:grid-cols-2 lg:gap-12">
        <section className="space-y-5">
          <div>
            <h2 className="text-18 font-medium text-primary">Google-provided details for Plane</h2>
            <p className="mt-1 text-13 text-tertiary">
              Create a Web application OAuth client in a Google Cloud project used only for this Calendar integration.
            </p>
          </div>

          {formFields.map((field) => (
            <ControllerInput
              key={field.key}
              control={control}
              type={field.type}
              name={field.key}
              label={field.label}
              description={field.description}
              placeholder={field.placeholder}
              error={field.error}
              required={field.required}
            />
          ))}

          <Controller
            control={control}
            name="GOOGLE_CALENDAR_IS_PROJECT_DEDICATED"
            rules={{ validate: (value) => value === "1" || "A dedicated Google Cloud project is required." }}
            render={({ field: { onChange, ref, value } }) => (
              <div className="space-y-1">
                <div className="flex items-start gap-2">
                  <Checkbox
                    id="GOOGLE_CALENDAR_IS_PROJECT_DEDICATED"
                    ref={ref}
                    checked={value === "1"}
                    onChange={(event) => onChange(event.target.checked ? "1" : "0")}
                    className="mt-0.5"
                  />
                  <label
                    htmlFor="GOOGLE_CALENDAR_IS_PROJECT_DEDICATED"
                    className="cursor-pointer text-13 font-medium text-secondary"
                  >
                    I confirm this Google Cloud project is dedicated to Plane's Google Calendar integration.
                  </label>
                </div>
                {errors.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED?.message && (
                  <p className="text-11 text-danger-primary">{errors.GOOGLE_CALENDAR_IS_PROJECT_DEDICATED.message}</p>
                )}
              </div>
            )}
          />
        </section>

        <section className="space-y-5">
          <div>
            <h2 className="text-18 font-medium text-primary">Plane-provided details for Google</h2>
            <p className="mt-1 text-13 text-tertiary">
              Add this exact value to the OAuth client's <CodeBlock darkerShade>Authorized redirect URIs</CodeBlock>.
            </p>
          </div>
          <div className="rounded-lg bg-layer-1 px-6 py-4">
            <CopyField
              label="Callback URI"
              url={callbackUri}
              description="Plane uses this callback to complete Google Calendar authorization."
            />
          </div>

          <div className="rounded-lg border border-subtle bg-layer-1 p-5">
            <div className="mb-3 flex items-center gap-2">
              <CalendarCheck className="size-4 text-accent-primary" />
              <h3 className="text-14 font-medium text-primary">Google Cloud setup</h3>
            </div>
            <ol className="list-decimal space-y-2 pl-5 text-13 text-secondary">
              <li>Enable the Google Calendar API for the dedicated project.</li>
              <li>
                Configure the OAuth consent screen with the app name, support and developer contact details, and the
                Calendar scopes Plane requests.
              </li>
              <li>
                Publish the consent screen when ready. Google may require verification before external users can
                authorize sensitive Calendar scopes.
              </li>
              <li>
                Plane requests offline consent so it can refresh access and keep calendars synchronized when members are
                away.
              </li>
            </ol>
          </div>
        </section>
      </div>

      <div className="flex gap-3 rounded-lg border border-warning-subtle bg-warning-subtle p-4 text-13 text-secondary">
        <CircleAlert className="mt-0.5 size-4 shrink-0 text-warning-primary" />
        <div className="space-y-2">
          <p>
            Self-hosted instances can show Google's <span className="font-medium text-primary">unverified app</span>{" "}
            interstitial until the OAuth consent screen is verified. This warning is controlled by Google, not Plane.
          </p>
          <p>
            Live credential rotation is unsupported. Revoking or deleting this dedicated OAuth client is a project-wide
            action that disconnects Google Calendar for every member, so coordinate revocation before replacing
            credentials.
          </p>
        </div>
      </div>

      <Button type="submit" variant="primary" size="lg" loading={isSubmitting} disabled={!isDirty}>
        {isSubmitting ? "Saving" : "Save changes"}
      </Button>
    </form>
  );
}
