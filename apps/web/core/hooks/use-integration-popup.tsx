/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";

const INTEGRATION_POPUP_CALLBACK_MESSAGE = "plane:integration-popup-callback";

type TIntegrationPopupCallbackMessage = {
  type: typeof INTEGRATION_POPUP_CALLBACK_MESSAGE;
  callbackUrl: string;
};

const isIntegrationPopupCallbackMessage = (data: unknown): data is TIntegrationPopupCallbackMessage =>
  typeof data === "object" &&
  data !== null &&
  "type" in data &&
  data.type === INTEGRATION_POPUP_CALLBACK_MESSAGE &&
  "callbackUrl" in data &&
  typeof data.callbackUrl === "string";

export const reportIntegrationPopupCallback = () => {
  if (!window.opener) return;

  window.opener.postMessage(
    {
      type: INTEGRATION_POPUP_CALLBACK_MESSAGE,
      callbackUrl: window.location.href,
    } satisfies TIntegrationPopupCallbackMessage,
    window.location.origin
  );
};

const useIntegrationPopup = ({
  provider,
  authUrl,
  onClose,
  stateParams,
  github_app_name,
  slack_client_id,
}: {
  provider?: string;
  authUrl?: string;
  onClose?: (callbackUrl?: string) => void;
  stateParams?: string;
  github_app_name?: string;
  slack_client_id?: string;
}) => {
  const [authLoader, setAuthLoader] = useState(false);

  const { workspaceSlug, projectId } = useParams();

  const providerUrls: { [key: string]: string } = {
    github: `https://github.com/apps/${github_app_name}/installations/new?state=${workspaceSlug?.toString()}`,
    slack: `https://slack.com/oauth/v2/authorize?scope=chat:write,im:history,im:write,links:read,links:write,users:read,users:read.email&amp;user_scope=&amp;&client_id=${slack_client_id}&state=${workspaceSlug?.toString()}`,
    slackChannel: `https://slack.com/oauth/v2/authorize?scope=incoming-webhook&client_id=${slack_client_id}&state=${workspaceSlug?.toString()},${projectId?.toString()}${
      stateParams ? "," + stateParams : ""
    }`,
  };

  const popup = useRef<Window | null>(null);
  const pollingInterval = useRef<number | null>(null);
  const closeTimeout = useRef<number | null>(null);
  const popupMessageListener = useRef<((event: MessageEvent) => void) | null>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const clearPopupPolling = useCallback(() => {
    if (pollingInterval.current !== null) {
      window.clearInterval(pollingInterval.current);
      pollingInterval.current = null;
    }

    if (closeTimeout.current !== null) {
      window.clearTimeout(closeTimeout.current);
      closeTimeout.current = null;
    }

    if (popupMessageListener.current) {
      window.removeEventListener("message", popupMessageListener.current);
      popupMessageListener.current = null;
    }
  }, []);

  const checkPopup = (openedPopup: Window) => {
    let hasHandledClose = false;
    let lastAccessibleUrl: string | undefined;
    let reportedCallbackUrl: string | undefined;

    clearPopupPolling();
    popupMessageListener.current = (event: MessageEvent) => {
      if (
        event.origin !== window.location.origin ||
        event.source !== openedPopup ||
        !isIntegrationPopupCallbackMessage(event.data)
      )
        return;

      reportedCallbackUrl = event.data.callbackUrl;
    };
    window.addEventListener("message", popupMessageListener.current);

    pollingInterval.current = window.setInterval(() => {
      if (!openedPopup.closed) {
        try {
          lastAccessibleUrl = openedPopup.location.href;
        } catch {
          // The provider page is cross-origin until it redirects back to Plane.
        }
      }

      if (openedPopup.closed && !hasHandledClose) {
        hasHandledClose = true;
        if (pollingInterval.current !== null) {
          window.clearInterval(pollingInterval.current);
          pollingInterval.current = null;
        }
        // Let a callback message already queued by the popup run before removing the listener.
        closeTimeout.current = window.setTimeout(() => {
          clearPopupPolling();
          popup.current = null;
          setAuthLoader(false);
          onCloseRef.current?.(reportedCallbackUrl ?? lastAccessibleUrl);
        }, 0);
      }
    }, 1000);
  };

  const openPopup = () => {
    if (!authUrl && !provider) return null;

    const width = 600,
      height = 600;
    const left = window.innerWidth / 2 - width / 2;
    const top = window.innerHeight / 2 - height / 2;
    const url = authUrl ?? providerUrls[provider ?? ""];

    if (!url) return null;

    return window.open(url, "", `width=${width}, height=${height}, top=${top}, left=${left}`);
  };

  const startAuth = () => {
    if (popup.current && !popup.current.closed) {
      popup.current.focus();
      return;
    }

    const openedPopup = openPopup();
    if (!openedPopup) {
      popup.current = null;
      clearPopupPolling();
      setAuthLoader(false);
      return;
    }

    popup.current = openedPopup;
    setAuthLoader(true);
    checkPopup(openedPopup);
  };

  useEffect(() => clearPopupPolling, [clearPopupPolling]);

  return {
    startAuth,
    isConnecting: authLoader,
  };
};

export default useIntegrationPopup;
