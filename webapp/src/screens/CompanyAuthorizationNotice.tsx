import { useTranslation } from 'react-i18next';
import { useCompanyAuthorizationDelivery } from '../domain/companyAuthorizationDelivery';

export function CompanyAuthorizationNotice() {
  const delivery = useCompanyAuthorizationDelivery();
  const { i18n } = useTranslation();
  if (!delivery?.pending) return null;
  return <p role="status" className="ae-banner ae-banner-warning">{i18n.language.startsWith('zh')
    ? '授权变更已保存在本机，正在等待公司服务确认。确认完成前请勿将此视为退出完成。'
    : 'The authorization change is saved on this device. Waiting for the company service to confirm; sign-out is not yet complete.'}</p>;
}
