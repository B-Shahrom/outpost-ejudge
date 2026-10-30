from outpost.similarity import compare, tokens

ORIGINAL = """
#include <bits/stdc++.h>
using namespace std;
int main() {
    int n; cin >> n;
    vector<long long> a(n);
    for (int i = 0; i < n; i++) cin >> a[i];
    long long best = LLONG_MIN, cur = 0;
    for (int i = 0; i < n; i++) {
        cur = max(a[i], cur + a[i]);
        best = max(best, cur);
    }
    cout << best << "\\n";
}
"""

# Same program: renamed, recommented, reformatted.
DISGUISED = """
#include <iostream>
#include <vector>
#include <climits>
using namespace std;
// my own solution :)
int main(){int len;cin>>len;
  vector<long long> arr(len);   /* read input */
  for(int j=0;j<len;j++) cin>>arr[j];
  long long answer=LLONG_MIN,run=0;
  for(int j=0;j<len;j++){ run=max(arr[j],run+arr[j]); answer=max(answer,run); }
  cout<<answer<<"\\n";
}
"""

# Same task, different idea (prefix sums and a running minimum).
DIFFERENT = """
#include <bits/stdc++.h>
int main() {
    int n; std::cin >> n;
    std::vector<long long> pre(n + 1, 0);
    for (int i = 1; i <= n; ++i) { long long x; std::cin >> x; pre[i] = pre[i - 1] + x; }
    long long lowest = 0, result = pre[1];
    for (int i = 1; i <= n; ++i) {
        result = std::max(result, pre[i] - lowest);
        lowest = std::min(lowest, pre[i]);
    }
    printf("%lld\\n", result);
}
"""


def test_tokens_ignore_names_comments_and_layout():
    assert tokens(ORIGINAL) == tokens(DISGUISED)


def test_disguised_copy_is_found_and_different_solution_is_not():
    pairs = compare({1: ORIGINAL, 2: DISGUISED, 3: DIFFERENT})
    assert [(p.a, p.b) for p in pairs] == [(1, 2)]
    assert pairs[0].score >= 0.9


def test_a_shared_template_does_not_make_everyone_alike():
    template = """
    #include <bits/stdc++.h>
    using namespace std;
    typedef long long ll; typedef pair<int,int> pii;
    template<class T> void chmax(T& a, T b) { if (b > a) a = b; }
    template<class T> void chmin(T& a, T b) { if (b < a) a = b; }
    void fast() { ios::sync_with_stdio(false); cin.tie(nullptr); cout.tie(nullptr); }
    int gcd_(int a, int b) { return b ? gcd_(b, a % b) : a; }
    """
    cores = [
        "int main(){fast(); int n; cin>>n; ll s=0; for(int i=0;i<n;i++){ll x;cin>>x;s+=x;} cout<<s;}",
        "int main(){fast(); ll a,b,c; cin>>a>>b>>c; cout<<max({a,b,c})-min({a,b,c});}",
        "int main(){fast(); string s; cin>>s; reverse(s.begin(), s.end()); cout<<s<<'\\n';}",
        "int main(){fast(); int n; cin>>n; vector<int> v(n); for(auto&x:v)cin>>x; sort(v.begin(),v.end()); cout<<v[n/2];}",
        'int main(){fast(); int q; cin>>q; while(q--){int x; cin>>x; cout<<(x%2?"odd":"even")<<"\\n";}}',
    ]
    assert compare({i: template + c for i, c in enumerate(cores)}) == []


def test_identical_groups_become_one_cluster_and_languages_stay_apart():
    from outpost.similarity import clusters
    sols = {i: ORIGINAL for i in range(1, 6)}
    sols[6] = DIFFERENT
    langs = {i: "cpp" for i in sols}
    sols[7], langs[7] = ORIGINAL, "java21"   # same text, other language: not compared
    found = clusters(compare(sols, langs))
    assert len(found) == 1 and found[0]["members"] == [1, 2, 3, 4, 5]
    assert len(found[0]["pairs"]) == 10
