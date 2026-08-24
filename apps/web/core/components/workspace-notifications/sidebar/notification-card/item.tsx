/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useState } from "react";
import { observer } from "mobx-react";
import { CalendarX2, Clock } from "lucide-react";
// plane imports
import { useTranslation } from "@plane/i18n";
import { Avatar, Row } from "@plane/ui";
import { cn, calculateTimeAgo, renderFormattedDate, renderFormattedTime, getFileURL } from "@plane/utils";
// hooks
import { useAppRouter } from "@/hooks/use-app-router";
import { useWorkspaceNotifications } from "@/hooks/store/notifications";
import { useNotification } from "@/hooks/store/notifications/use-notification";
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
import { useWorkspace } from "@/hooks/store/use-workspace";
// local imports
import { NotificationContent } from "./content";
import { NotificationOption } from "./options";

type TNotificationItem = {
  workspaceSlug: string;
  notificationId: string;
};

export const NotificationItem = observer(function NotificationItem(props: TNotificationItem) {
  const { workspaceSlug, notificationId } = props;
  // hooks
  const router = useAppRouter();
  const { t } = useTranslation();
  const { currentSelectedNotificationId, setCurrentSelectedNotificationId } = useWorkspaceNotifications();
  const { asJson: notification, markNotificationAsRead } = useNotification(notificationId);
  const { getIsIssuePeeked, setPeekIssue } = useIssueDetail();
  const { getWorkspaceBySlug } = useWorkspace();
  // states
  const [isSnoozeStateModalOpen, setIsSnoozeStateModalOpen] = useState(false);
  const [customSnoozeModal, setCustomSnoozeModal] = useState(false);

  // derived values
  const projectId = notification?.project || undefined;
  const notificationData = notification?.data;
  const calendarConnection =
    notificationData && "google_calendar_connection" in notificationData
      ? notificationData.google_calendar_connection
      : undefined;
  const issueData = notificationData && "issue_activity" in notificationData ? notificationData : undefined;
  const issueId = issueData?.issue?.id || undefined;
  const workspace = getWorkspaceBySlug(workspaceSlug);

  const notificationField = issueData?.issue_activity.field || undefined;
  const notificationTriggeredBy = notification.triggered_by_details || undefined;

  const handleCalendarNotification = async () => {
    if (!calendarConnection || isSnoozeStateModalOpen || customSnoozeModal) return;

    if (notification.read_at === null) {
      try {
        await markNotificationAsRead(workspaceSlug);
      } catch (error) {
        console.error(error);
      }
    }

    const querySeparator = calendarConnection.action_url.includes("?") ? "&" : "?";
    router.push(
      `${calendarConnection.action_url}${querySeparator}connection_error=${encodeURIComponent(calendarConnection.error)}`
    );
  };

  const handleNotificationIssuePeekOverview = async () => {
    if (workspaceSlug && projectId && issueId && !isSnoozeStateModalOpen && !customSnoozeModal) {
      setPeekIssue(undefined);
      setCurrentSelectedNotificationId(notificationId);

      // make the notification as read
      if (notification.read_at === null) {
        try {
          await markNotificationAsRead(workspaceSlug);
        } catch (error) {
          console.error(error);
        }
      }

      if (notification?.is_inbox_issue === false) {
        if (!getIsIssuePeeked(issueId)) {
          setPeekIssue({ workspaceSlug, projectId, issueId });
        }
      }
    }
  };

  if (!workspaceSlug || !notificationId || !notification?.id) return <></>;

  if (calendarConnection)
    return (
      <Row
        className={cn(
          "group relative flex cursor-pointer items-center gap-2 border-b border-subtle py-4 transition-all",
          {
            "bg-accent-primary/5": notification.read_at === null,
          }
        )}
        onClick={() => void handleCalendarNotification()}
      >
        {notification.read_at === null && (
          <div className="absolute top-[50%] left-2 h-1.5 w-1.5 flex-shrink-0 rounded-full bg-accent-primary" />
        )}

        <div className="relative flex w-full gap-2">
          <div className="relative flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-full bg-layer-1">
            <CalendarX2 className="size-5 text-tertiary" />
          </div>

          <div className="-mt-2 w-full space-y-1">
            <div className="relative flex h-8 items-center gap-3">
              <div className="line-clamp-1 w-full truncate overflow-hidden text-body-xs-medium break-all whitespace-normal text-primary">
                {t("notification.google_calendar.connection_broken.title")}
              </div>
              <NotificationOption
                workspaceSlug={workspaceSlug}
                notificationId={notification.id}
                isSnoozeStateModalOpen={isSnoozeStateModalOpen}
                setIsSnoozeStateModalOpen={setIsSnoozeStateModalOpen}
                customSnoozeModal={customSnoozeModal}
                setCustomSnoozeModal={setCustomSnoozeModal}
              />
            </div>

            <div className="relative flex items-center gap-3 text-caption-sm-regular text-secondary">
              <div className="line-clamp-1 w-full truncate overflow-hidden break-words whitespace-normal">
                {t(
                  calendarConnection.error === "oauth_credentials_changed"
                    ? "notification.google_calendar.connection_broken.credentials_changed"
                    : "notification.google_calendar.connection_broken.reconnect"
                )}
              </div>
              <p className="mt-auto flex-shrink-0 text-tertiary">
                {notification.created_at && calculateTimeAgo(notification.created_at)}
              </p>
            </div>
          </div>
        </div>
      </Row>
    );

  if (!notificationField || !issueData || !workspace?.id || !projectId) return <></>;

  return (
    <Row
      className={cn(
        "group relative flex cursor-pointer items-center gap-2 border-b border-subtle py-4 transition-all",
        {
          "bg-layer-1/30": currentSelectedNotificationId === notification?.id,
          "bg-accent-primary/5": notification.read_at === null,
        }
      )}
      onClick={handleNotificationIssuePeekOverview}
    >
      {notification.read_at === null && (
        <div className="absolute top-[50%] left-2 h-1.5 w-1.5 flex-shrink-0 rounded-full bg-accent-primary" />
      )}

      <div className="relative flex w-full gap-2">
        <div className="relative flex h-12 w-12 flex-shrink-0 items-center justify-center rounded-full bg-layer-1">
          {notificationTriggeredBy && (
            <Avatar
              name={notificationTriggeredBy.display_name || notificationTriggeredBy?.first_name}
              src={getFileURL(notificationTriggeredBy.avatar_url)}
              size={42}
              shape="circle"
              className="bg-layer-1 text-body-sm-medium"
            />
          )}
        </div>

        <div className="-mt-2 w-full space-y-1">
          <div className="relative flex h-8 items-center gap-3">
            <div className="line-clamp-1 w-full truncate overflow-hidden text-body-xs-medium break-all whitespace-normal text-primary">
              <NotificationContent
                notification={notification}
                workspaceId={workspace.id}
                workspaceSlug={workspaceSlug}
                projectId={projectId}
              />
            </div>
            <NotificationOption
              workspaceSlug={workspaceSlug}
              notificationId={notification?.id}
              isSnoozeStateModalOpen={isSnoozeStateModalOpen}
              setIsSnoozeStateModalOpen={setIsSnoozeStateModalOpen}
              customSnoozeModal={customSnoozeModal}
              setCustomSnoozeModal={setCustomSnoozeModal}
            />
          </div>

          <div className="relative flex items-center gap-3 text-caption-sm-regular text-secondary">
            <div className="line-clamp-1 w-full truncate overflow-hidden break-words whitespace-normal">
              {issueData.issue?.identifier}-{issueData.issue?.sequence_id}&nbsp;
              {issueData.issue?.name}
            </div>
            <div className="flex-shrink-0">
              {notification?.snoozed_till ? (
                <p className="flex flex-shrink-0 items-center justify-end gap-x-1 text-tertiary">
                  <Clock className="h-4 w-4" />
                  <span>
                    Till {renderFormattedDate(notification.snoozed_till)},&nbsp;
                    {renderFormattedTime(notification.snoozed_till, "12-hour")}
                  </span>
                </p>
              ) : (
                <p className="mt-auto flex-shrink-0 text-tertiary">
                  {notification.created_at && calculateTimeAgo(notification.created_at)}
                </p>
              )}
            </div>
          </div>
        </div>
      </div>
    </Row>
  );
});
