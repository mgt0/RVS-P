/* =============================================================
   부트스트랩 — 셸을 마운트하고 폴링을 시작한다. 마지막에 로드된다.
   rv-* 공용 컴포넌트를 전역 등록하므로 화면 템플릿은 import 없이 <rv-status> 처럼 쓴다.
   라우트 연결·keep-alive·알림 상세 창은 shell.js, 상태·폴링은 core/store.js.
   ============================================================= */
(function () {
  const RVS = window.RVS;
  const app = Vue.createApp(RVS.Shell);
  RVS.ui.install(app);
  app.mount('#app');
  RVS.store.start();
})();
