"""Sentleaf account emails: inline styles, real text, and direct action links."""
from html import escape


def _layout(*, preview, label, heading, intro, action, link, note, fallback=False, details=()):
    # Escape every value, including both copies of the URL. Never place account
    # links in image requests or external fonts; the design needs no remote assets.
    preview, label, heading, intro, action, link, note = (
        escape(value, quote=True)
        for value in (preview, label, heading, intro, action, link, note)
    )
    extra = ''
    if details:
        rows = ''.join(
            '<tr><td style="padding:0 0 18px;word-break:break-word;overflow-wrap:anywhere;">'
            f'<p style="margin:0 0 5px;color:#677169;font-size:12px;">{escape(title)}</p>'
            f'<p style="margin:0;color:#202d27;font-size:14px;line-height:22px;white-space:pre-wrap;">{escape(value)}</p>'
            '</td></tr>' for title, value in details
        )
        extra = f'<tr><td class="inset" style="padding:0 32px 10px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="table-layout:fixed;">{rows}</table></td></tr>'
    if fallback:
        extra += f'''
          <tr><td class="inset" style="padding:0 32px 30px;">
            <p style="margin:0 0 8px;color:#677169;font-size:12px;line-height:18px;">Button not working? Copy this link into your browser:</p>
            <p style="margin:0;font-size:12px;line-height:18px;word-break:break-all;overflow-wrap:anywhere;">
              <a href="{link}" style="color:#244e40;text-decoration:underline;word-break:break-all;overflow-wrap:anywhere;">{link}</a>
            </p>
          </td></tr>'''
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>{heading} — Sentleaf</title>
  <style>
    body, table, td, a {{ -webkit-text-size-adjust:100%; -ms-text-size-adjust:100%; }}
    table, td {{ mso-table-lspace:0pt; mso-table-rspace:0pt; }}
    @media only screen and (max-width:480px) {{
      .outer {{ padding:20px 12px !important; }}
      .inset {{ padding-left:24px !important; padding-right:24px !important; }}
      .headline {{ font-size:34px !important; line-height:39px !important; }}
    }}
  </style>
</head>
<body style="margin:0;padding:0;width:100%;background-color:#f7f8f3;color:#202d27;font-family:Arial,Helvetica,sans-serif;">
  <div aria-hidden="true" style="display:none;font-size:1px;line-height:1px;color:#f7f8f3;max-height:0;max-width:0;opacity:0;overflow:hidden;mso-hide:all;">{preview}</div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f7f8f3" style="width:100%;background-color:#f7f8f3;">
    <tr><td class="outer" align="center" style="padding:36px 16px;">
      <!--[if mso]><table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"><tr><td><![endif]-->
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:560px;table-layout:fixed;">
        <tr><td style="padding:0 0 22px;color:#244e40;font-size:29px;line-height:34px;font-weight:bold;letter-spacing:-1.4px;">sentleaf<span style="color:#849a69;">.</span></td></tr>
        <tr><td bgcolor="#244e40" style="background-color:#244e40;border-radius:12px 12px 0 0;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;">
            <tr><td class="inset" style="padding:30px 32px 14px;color:#dbeaac;font-size:10px;line-height:16px;font-weight:bold;letter-spacing:2px;">{label}</td></tr>
            <tr><td class="inset" style="padding:0 32px 32px;">
              <h1 class="headline" style="margin:0;color:#f7f8f3;font-family:Georgia,'Times New Roman',serif;font-size:40px;line-height:46px;font-weight:normal;letter-spacing:-1px;">{heading}</h1>
            </td></tr>
          </table>
        </td></tr>
        <tr><td bgcolor="#dbeaac" height="4" style="height:4px;line-height:4px;font-size:0;background-color:#dbeaac;">&nbsp;</td></tr>
        <tr><td bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid #dfe4dc;border-top:0;border-radius:0 0 12px 12px;">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;table-layout:fixed;">
            <tr><td class="inset" style="padding:28px 32px 22px;">
              <p style="margin:0;color:#425148;font-size:15px;line-height:24px;">{intro}</p>
            </td></tr>
            <tr><td class="inset" style="padding:0 32px 24px;">
              <table role="presentation" cellpadding="0" cellspacing="0" border="0">
                <tr><td align="center" bgcolor="#244e40" style="border-radius:6px;background-color:#244e40;mso-padding-alt:16px 26px;">
                  <a href="{link}" style="display:inline-block;border:1px solid #244e40;border-radius:6px;padding:15px 25px;background-color:#244e40;color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:20px;font-weight:bold;text-decoration:none;text-align:center;mso-padding-alt:0;">{action}</a>
                </td></tr>
              </table>
            </td></tr>
            <tr><td class="inset" style="padding:0 32px 28px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#f1f4ed" style="width:100%;background-color:#f1f4ed;border-radius:6px;">
                <tr><td style="padding:14px 16px;color:#425148;font-size:13px;line-height:21px;">{note}</td></tr>
              </table>
            </td></tr>
            {extra}
          </table>
        </td></tr>
        <tr><td style="padding:22px 4px 0;color:#677169;font-size:12px;line-height:19px;">
          <p style="margin:0;">Sentleaf will never ask you to send your password by email.</p>
          <p style="margin:12px 0 0;color:#244e40;font-weight:bold;">A little peace of mind for your forms.</p>
        </td></tr>
      </table>
      <!--[if mso]></td></tr></table><![endif]-->
    </td></tr>
  </table>
</body>
</html>'''


def password_reset_html(link):
    return _layout(
        preview='Choose a new Sentleaf password. Your link is valid for 15 minutes.',
        label='ACCOUNT ACCESS',
        heading='Reset your password.',
        intro='You requested a new password for your Sentleaf account. Use the button below to choose one.',
        action='Reset password',
        link=link,
        note='This link expires in 15 minutes and can be used once. If you didn’t request it, you can ignore this email. Your password has not changed.',
        fallback=True,
    )


def password_changed_html(origin):
    return _layout(
        preview='Your Sentleaf password was updated. Previous sign-in sessions have ended.',
        label='ACCOUNT UPDATE',
        heading='Your password is updated.',
        intro='Your new password is ready to use. All previous sign-in sessions for this account have ended.',
        action='Sign in to Sentleaf',
        link=f'{origin}/login',
        note='If you didn’t make this change, secure your email account. Then use “Forgot password?” on the Sentleaf sign-in page to reset your password.',
    )


def inquiry_email(inquiry, origin):
    from datetime import datetime, timezone
    kind = {'access': 'Trial request', 'support': 'Support message', 'data': 'Data request'}[inquiry['kind']]
    received = datetime.fromtimestamp(inquiry['created'], timezone.utc).strftime('%d %b %Y, %H:%M UTC')
    link = f'{origin}/workspace#requests'
    details = [('From', inquiry['email']), ('Website', inquiry['website'] or 'Not provided'),
               ('Message', inquiry['message'] or 'No additional message.'), ('Received', received)]
    subject = f'[Sentleaf] {kind} - {inquiry["id"][:8]}'
    text = f'A new {kind.lower()} was submitted through Sentleaf.\n\n'
    text += '\n\n'.join(f'{label}:\n{value}' for label, value in details)
    text += f'\n\nView in your workspace:\n{link}\n\nReply to this email to contact the sender. The sender address is supplied by the visitor and is not verified.'
    html = _layout(preview=f'A new {kind.lower()} is ready for your review.', label='NEW ENQUIRY',
                   heading=f'New {kind.lower()}.', intro='A visitor sent you a message through Sentleaf.',
                   action='Open requests', link=link, details=details,
                   note='Reply to this email to contact the sender. The address is supplied by the visitor and is not verified.')
    return dict(subject=subject, text=text, html=html, reply_to=inquiry['email'])
