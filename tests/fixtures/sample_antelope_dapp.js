async function setPrice(session, oracle, user) {
  await session.contract.actions.setprice(oracle, { user, id: 7 }, [{ actor: user }]);
  const rows = await session.contract.tables.prices(oracle).getTableRows({ scope: oracle });
  return rows;
}

async function transfer(api) {
  await api.transact({
    actions: [{
      account: 'eosio.token',
      name: 'transfer',
      authorization: [],
      data: {}
    }]
  }, { blocksBehind: 3, expireSeconds: 30 });
}
