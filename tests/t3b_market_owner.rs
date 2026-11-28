//! Loopback-only finance API with the pinned finance tests' synthetic market owner.
//! The caller copies this test into an archive of the committed finance gitlink.

use finance_api::{AppState, router_with_market_reads};
use std::{io::Write, net::Ipv4Addr, path::PathBuf};

mod options {
    include!("market_options.rs");

    pub(super) fn sources() -> MarketReadSources {
        let depth = DepthFrames::default();
        depth.publish(DepthFrame {
            symbol: DepthSymbol::Qqq,
            permissions: DepthPermissions {
                equity_l1_confirmed: true,
                totalview_confirmed: true,
                eds_confirmed: true,
            },
            state: DepthState::Receiving,
            request_id: Some(17),
            requested_rows: 5,
            received_at: Some(AS_OF.parse().unwrap()),
            bids: vec![DepthLevel {
                price: PRICE.parse().unwrap(),
                size: "1.2500".parse().unwrap(),
            }],
            asks: Vec::new(),
        });
        let input = Arc::new(exposure_with_gamma_grid());
        let exposure: Arc<ExposureSource> = Arc::new(move |id, as_of| {
            (id == &input.chain.underlying_id && as_of.is_none_or(|time| time == input.chain.as_of))
                .then(|| input.clone())
        });
        MarketReadSources {
            option_lines: Some(licensed::<Arc<dyn OptionLineSource>>(Arc::new(
                ReplayLines::default(),
            ))),
            depth: Some(licensed(depth)),
            exposure: Some(licensed(exposure)),
            ..Default::default()
        }
    }
}

mod flow {
    include!("option_flow.rs");

    pub(super) fn attach(sources: &mut MarketReadSources) {
        sources.flow_observations = Some(observation_source(observations()));
        sources.flow_verifications = Some(verification_source(verification(false, false)));
    }
}

#[tokio::test]
#[ignore = "started and stopped by the isolated fertile integration test"]
async fn serve_owner() {
    let work = std::env::current_dir().unwrap();
    let root = |name| {
        let path = PathBuf::from(std::env::var_os(name).unwrap());
        assert!(path.canonicalize().unwrap().starts_with(&work));
        path
    };
    let mut sources = options::sources();
    flow::attach(&mut sources);
    let state = AppState {
        data_root: root("FERTILE_DATA_ROOT"),
        research_root: root("FERTILE_RESEARCH_ROOT"),
    };
    let listener = tokio::net::TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .await
        .unwrap();
    let mut terminate =
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).unwrap();
    println!("T3B_OWNER_LISTENING={}", listener.local_addr().unwrap());
    std::io::stdout().flush().unwrap();
    axum::serve(listener, router_with_market_reads(state, sources))
        .with_graceful_shutdown(async move {
            terminate.recv().await;
        })
        .await
        .unwrap();
}
