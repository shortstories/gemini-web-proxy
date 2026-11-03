window.__ModuleLoader__.load({ id: 'dsh-subscription-login', factory: (require) => {
  const React = require('react');
  function LoginPage() {
    return React.createElement('iframe', {
      src: '/subscription-login/', title: '구독 로그인',
      style: {width: '100%', height: '780px', border: 0, borderRadius: '12px'},
    });
  }
  return {
    inject: ['slots'],
    apply(ctx) {
      ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({
        name: 'plugins.bundle.config', key: 'dsh-subscription-login',
      }, LoginPage));
      ctx.slots.inject('plugins.detail.section', () => ctx.slots.register({
        name: 'plugins.detail.section', id: 'subscription-login', order: 10,
      }, ({subject}) => subject?.pkg?.name === 'dsh-subscriptions' ? React.createElement(LoginPage) : null));
      ctx.slots.inject('settings.section', () => ctx.slots.register({
        name: 'settings.section', id: 'subscriptions', order: 12, label: () => '구독 로그인',
      }, LoginPage));
    },
  };
}});
